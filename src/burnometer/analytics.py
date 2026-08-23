"""Rollups over the store.

Three rules are enforced here rather than left to each caller, because getting
any of them wrong produces a number that looks right and is not:

**Never sum across cost bases.** A user on a subscription is not billed per
token, so their figure is what the tokens *would* have cost. Adding that to a
real API charge produces a total that means nothing. Rows therefore carry a
basis, and totals are returned as a mapping keyed by basis — there is
deliberately no single fused number for a caller to reach for.

**Unpriced is not zero.** A model with no known rate keeps its full token
breakdown, reports ``cost_usd=None``, and is counted in ``unpriced_requests``.
Dropping it would understate; pricing it at zero would claim it was free.

**Keep the raw slug.** ``model`` is whatever the provider reported.
``model_family`` exists only for grouping, and never replaces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .models import CostBasis, TokenCounts
from .store import Store

__all__ = [
    "CacheEfficiency",
    "cache_efficiency",
    "TimeSeries",
    "SeriesPoint",
    "time_series",
    "BUCKETS",
    "Totals",
    "Row",
    "Report",
    "Block",
    "BlockReport",
    "DIMENSIONS",
    "aggregate",
    "blocks",
]

#: Whitelisted grouping expressions. Callers pass a key, never SQL — the values
#: below are the only strings that ever reach a GROUP BY clause.
DIMENSIONS: dict[str, str] = {
    "model": "model",
    "family": "model_family",
    "project": "COALESCE(project, '(unattributed)')",
    "provider": "provider",
    "day": "substr(ts, 1, 10)",
    "session": "COALESCE(session_id, '(none)')",
    "effort": "COALESCE(effort, '(default)')",
}


@dataclass(frozen=True, slots=True)
class Totals:
    requests: int = 0
    tokens: TokenCounts = field(default_factory=TokenCounts)
    cost_usd: float | None = None
    unpriced_requests: int = 0

    @property
    def input_side(self) -> int:
        """Every token that formed part of a prompt."""
        return self.tokens.input + self.tokens.cache_read + self.tokens.cache_write

    @property
    def cache_hit_rate(self) -> float | None:
        """Share of prompt tokens served from cache."""
        total = self.input_side
        return self.tokens.cache_read / total if total else None

    @property
    def effective_rate(self) -> float | None:
        """USD per million tokens, all-in.

        The number that actually distinguishes models in agentic use. List
        prices are close to meaningless when 96% of input is cache reads: Opus 5
        lists at $5/Mtok input and lands near $1 all-in.
        """
        if self.cost_usd is None or not self.tokens.total:
            return None
        return self.cost_usd / self.tokens.total * 1_000_000


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    basis: CostBasis
    totals: Totals
    provider: str | None = None
    price_source: str | None = None

    @property
    def is_unpriced(self) -> bool:
        return self.basis is CostBasis.UNPRICED


@dataclass(slots=True)
class Report:
    dimension: str
    rows: list[Row]
    subtotals: dict[CostBasis, Totals]
    """Keyed by basis on purpose. There is no combined total, because combining
    real charges with counterfactual ones would be meaningless."""

    unpriced_models: set[str] = field(default_factory=set)
    since: datetime | None = None
    until: datetime | None = None

    @property
    def total_requests(self) -> int:
        return sum(t.requests for t in self.subtotals.values())

    def share_of_basis(self, row: Row) -> float | None:
        """A row's share of spend *within its own basis*."""
        sub = self.subtotals.get(row.basis)
        if sub is None or not sub.cost_usd or row.totals.cost_usd is None:
            return None
        return row.totals.cost_usd / sub.cost_usd


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def aggregate(
    store: Store,
    dimension: str = "model",
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    limit: int | None = None,
) -> Report:
    """Group stored events along one dimension.

    ``dimension`` is a key of :data:`DIMENSIONS`; anything else is rejected, so
    no caller-supplied string reaches the query. ``model`` narrows to one model,
    which is how "where did this model's spend go" is answered — the value is
    bound, never interpolated.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; expected one of {sorted(DIMENSIONS)}")
    group_expr = DIMENSIONS[dimension]

    where = ["1=1"]
    params: list[object] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("ts < ?")
        params.append(_iso(until))
    if provider is not None:
        where.append("provider = ?")
        params.append(provider)
    if model is not None:
        where.append("model = ?")
        params.append(model)

    # sql-audited: group_expr comes from the DIMENSIONS whitelist above and can
    # never be caller-supplied; every value is bound.
    sql = f"""
        SELECT {group_expr} AS key,
               cost_basis,
               MIN(provider) AS provider,
               MAX(price_source) AS price_source,
               COUNT(*) AS requests,
               SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_requests,
               SUM(cost_usd) AS cost_usd,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(reasoning_tokens) AS reasoning_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_5m_tokens) AS cache_write_5m_tokens,
               SUM(cache_write_1h_tokens) AS cache_write_1h_tokens
        FROM usage_events
        WHERE {" AND ".join(where)}
        GROUP BY key, cost_basis
        ORDER BY cost_usd DESC NULLS LAST, requests DESC
    """  # sql-audited

    rows: list[Row] = []
    subtotals: dict[CostBasis, Totals] = {}
    unpriced_models: set[str] = set()

    for raw in store.query(sql, tuple(params)):
        basis = CostBasis(raw["cost_basis"])
        tokens = TokenCounts(
            input=raw["input_tokens"] or 0,
            output=raw["output_tokens"] or 0,
            reasoning=raw["reasoning_tokens"] or 0,
            cache_read=raw["cache_read_tokens"] or 0,
            cache_write_5m=raw["cache_write_5m_tokens"] or 0,
            cache_write_1h=raw["cache_write_1h_tokens"] or 0,
        )
        totals = Totals(
            requests=raw["requests"],
            tokens=tokens,
            cost_usd=raw["cost_usd"],
            unpriced_requests=raw["unpriced_requests"] or 0,
        )
        row = Row(
            key=str(raw["key"]),
            basis=basis,
            totals=totals,
            provider=raw["provider"],
            price_source=raw["price_source"],
        )
        rows.append(row)
        if basis is CostBasis.UNPRICED and dimension == "model":
            unpriced_models.add(row.key)

        prev = subtotals.get(basis, Totals())
        subtotals[basis] = Totals(
            requests=prev.requests + totals.requests,
            tokens=prev.tokens + totals.tokens,
            cost_usd=(
                None
                if totals.cost_usd is None and prev.cost_usd is None
                else (prev.cost_usd or 0.0) + (totals.cost_usd or 0.0)
            ),
            unpriced_requests=prev.unpriced_requests + totals.unpriced_requests,
        )

    if limit is not None:
        rows = rows[:limit]

    return Report(
        dimension=dimension,
        rows=rows,
        subtotals=subtotals,
        unpriced_models=unpriced_models,
        since=since,
        until=until,
    )


# --------------------------------------------------------------------------
# Rolling usage windows
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    """One rolling usage window.

    Claude's rate limit runs on a five-hour window that opens with the first
    request. We can derive the window and what was consumed inside it exactly.
    We cannot derive **how full it is**: Anthropic does not publish a token
    limit for subscription plans, and Claude Code stores no quota data on disk
    (verified across every transcript and CLI subcommand). So this type reports
    consumption and timing, and deliberately exposes no percentage — see
    :meth:`BlockReport.relative_to_history` for the honest alternative.
    """

    start: datetime
    """The first request in the window — an observed fact, not a rounded one."""

    end: datetime
    requests: int
    tokens: TokenCounts
    cost_usd: float | None
    basis: CostBasis
    window_hours: int = 5

    @property
    def expires_at(self) -> datetime:
        """When the window is expected to reset.

        Derived, not observed. Anthropic does not publish whether the window is
        anchored to the first request or rounded to the hour, so this assumes
        the former — the only anchor we can actually see. Treat it as accurate
        to within an hour, and label it as an estimate wherever it is shown.
        """
        return self.start + timedelta(hours=self.window_hours)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.start <= now < self.expires_at

    def remaining(self, now: datetime | None = None) -> timedelta:
        now = now or datetime.now(UTC)
        return max(self.expires_at - now, timedelta(0))


@dataclass(slots=True)
class BlockReport:
    blocks: list[Block]
    window_hours: int = 5

    @property
    def current(self) -> Block | None:
        return self.blocks[-1] if self.blocks and self.blocks[-1].is_active() else None

    def relative_to_history(self, block: Block) -> float | None:
        """How this block compares to the user's own past blocks.

        This is what we show instead of a percentage of some limit. Comparing a
        block against the user's own history is a claim we can actually support;
        "58% of your limit" would require a denominator nobody publishes and we
        would be inventing it.

        Returns the ratio to the median completed block's cost, or ``None`` when
        there is not enough history to say anything.
        """
        past = [b.cost_usd for b in self.blocks if b is not block and b.cost_usd]
        if len(past) < 3 or not block.cost_usd:
            return None
        past.sort()
        median = past[len(past) // 2]
        return block.cost_usd / median if median else None


def blocks(
    store: Store,
    *,
    provider: str = "claude_code",
    window_hours: int = 5,
    since: datetime | None = None,
) -> BlockReport:
    """Group events into rolling windows.

    A new window opens when the previous one has run its full length, or when
    the gap since the last request exceeds the window — an idle gap that long
    means the earlier window expired untouched.
    """
    where = ["provider = ?"]
    params: list[object] = [provider]
    if since is not None:
        where.append("ts >= ?")
        params.append(_iso(since))

    sql = (
        "SELECT ts, cost_usd, cost_basis, input_tokens, output_tokens, reasoning_tokens, "
        "cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens "
        f"FROM usage_events WHERE {' AND '.join(where)} ORDER BY ts"  # sql-audited
    )

    span = timedelta(hours=window_hours)
    out: list[Block] = []
    start: datetime | None = None
    last: datetime | None = None
    acc = TokenCounts()
    cost: float | None = None
    requests = 0
    basis = CostBasis.UNPRICED

    def flush() -> None:
        nonlocal start, acc, cost, requests, basis
        if start is not None and requests:
            out.append(
                Block(
                    start=start,
                    end=last or start,
                    requests=requests,
                    tokens=acc,
                    cost_usd=cost,
                    basis=basis,
                    window_hours=window_hours,
                )
            )
        start, acc, cost, requests = None, TokenCounts(), None, 0

    for raw in store.query(sql, tuple(params)):
        ts = datetime.fromisoformat(raw["ts"].replace("Z", "+00:00"))
        if start is None or ts - start >= span or (last is not None and ts - last >= span):
            flush()
            # Anchored to the actual first request. An earlier version floored
            # this to the hour, which is a rounding rule we cannot verify and
            # which moves the projected reset by up to 59 minutes. Using the
            # observed timestamp keeps the one fact we have.
            start = ts
        acc = acc + TokenCounts(
            input=raw["input_tokens"] or 0,
            output=raw["output_tokens"] or 0,
            reasoning=raw["reasoning_tokens"] or 0,
            cache_read=raw["cache_read_tokens"] or 0,
            cache_write_5m=raw["cache_write_5m_tokens"] or 0,
            cache_write_1h=raw["cache_write_1h_tokens"] or 0,
        )
        if raw["cost_usd"] is not None:
            cost = (cost or 0.0) + raw["cost_usd"]
        basis = CostBasis(raw["cost_basis"])
        requests += 1
        last = ts

    flush()
    return BlockReport(blocks=out, window_hours=window_hours)


def latest_quotas(store: Store, provider: str) -> Sequence[object]:
    """Most recent exact quota reading per window, if the provider records one."""
    return store.latest_quota(provider)


# --------------------------------------------------------------------------
# Time series for charts
# --------------------------------------------------------------------------

#: Whitelisted bucket expressions. As with DIMENSIONS, a caller passes a key and
#: never SQL, so nothing caller-supplied reaches a GROUP BY.
BUCKETS: dict[str, str] = {
    "hour": "substr(ts, 1, 13)",
    "day": "substr(ts, 1, 10)",
}


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One bucket of a chart, split by provider.

    The split is carried rather than pre-summed because provider is the identity
    a reader wants to see in a stacked bar — "how much of today was Claude" is a
    different question from "how much was today".
    """

    label: str
    by_provider: dict[str, float]

    @property
    def total(self) -> float:
        return sum(self.by_provider.values())


@dataclass(slots=True)
class TimeSeries:
    bucket: str
    points: list[SeriesPoint]
    providers: list[str]
    """Every provider appearing anywhere in the series, in stable order, so a
    chart can assign colours once and not repaint when a bucket lacks one."""

    @property
    def total(self) -> float:
        return sum(point.total for point in self.points)

    @property
    def peak(self) -> float:
        return max((point.total for point in self.points), default=0.0)


def time_series(
    store: Store,
    *,
    bucket: str = "day",
    since: datetime | None = None,
    until: datetime | None = None,
    fill: bool = True,
) -> TimeSeries:
    """Cost per time bucket, split by provider.

    ``fill`` inserts empty buckets for periods with no usage. Without it a chart
    would silently compress idle days out of existence, making a burst look like
    steady work.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {sorted(BUCKETS)}")

    where = ["1=1"]
    params: list[object] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("ts < ?")
        params.append(_iso(until))

    # sql-audited: the bucket expression comes from the BUCKETS whitelist; every
    # value is bound.
    sql = f"""
        SELECT {BUCKETS[bucket]} AS bucket, provider, SUM(cost_usd) AS cost
        FROM usage_events
        WHERE {" AND ".join(where)}
        GROUP BY bucket, provider
        ORDER BY bucket
    """  # sql-audited

    grouped: dict[str, dict[str, float]] = {}
    providers: list[str] = []
    for row in store.query(sql, tuple(params)):
        label = str(row["bucket"])
        provider = row["provider"]
        grouped.setdefault(label, {})[provider] = row["cost"] or 0.0
        if provider not in providers:
            providers.append(provider)

    labels = sorted(grouped)
    if fill and since is not None:
        labels = _bucket_labels(bucket, since, until or datetime.now(UTC))

    points = [SeriesPoint(label=label, by_provider=grouped.get(label, {})) for label in labels]
    return TimeSeries(bucket=bucket, points=points, providers=sorted(providers))


def _bucket_labels(bucket: str, since: datetime, until: datetime) -> list[str]:
    """Every bucket label in a range, including empty ones."""
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    width = 13 if bucket == "hour" else 10
    cursor = since.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        cursor = cursor.replace(hour=0)
    out: list[str] = []
    while cursor <= until:
        out.append(_iso(cursor)[:width])
        cursor += step
    return out


# --------------------------------------------------------------------------
# Cache efficiency
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheEfficiency:
    """How much of the prompt side came from cache, and what that was worth.

    Worth its own figure because it is the single biggest lever on cost in
    agentic use and it is invisible in a spend total. A 96% hit rate is the
    difference between Opus 5 costing its $5/Mtok list rate and costing about
    $1 all-in.
    """

    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    fresh_input: int = 0
    savings_usd: float | None = None
    """The **net** saving: what the same work would have cost with no caching,
    minus what it actually cost.

    Net matters. An earlier version counted only what cache *reads* saved, which
    overstates it — cache *writes* cost more than fresh input, 1.25x for 5-minute
    TTL and 2.0x for 1-hour, so part of the read saving is spent buying it. A
    figure that ignores the premium is the kind of flattering-but-wrong number
    this tool exists to avoid.

    ``None`` when any model involved is unpriced; a partial figure would read as
    fact."""

    actual_usd: float | None = None
    """What the period actually came to."""

    without_cache_usd: float | None = None
    """What the same work would have cost with no prompt caching at all — every
    input token billed at the full rate. The counterfactual is what makes the
    saving legible: "87% saved" means something, "$267 saved" means more."""

    @property
    def input_side(self) -> int:
        return self.fresh_input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    @property
    def hit_rate(self) -> float | None:
        total = self.input_side
        return self.cache_read / total if total else None


def cache_efficiency(
    store: Store,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> CacheEfficiency:
    """Cache totals and the money the cache saved.

    The saving is computed per model, because the gap between a model's input
    rate and its cache-read rate is what generates it, and those differ. Any
    unpriced model makes the whole figure ``None`` rather than a quiet
    undercount.
    """
    from .pricing import load_catalog

    where = ["1=1"]
    params: list[object] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("ts < ?")
        params.append(_iso(until))

    sql = (
        "SELECT model, SUM(input_tokens) AS fresh, SUM(cache_read_tokens) AS cache_read, "
        "SUM(cache_write_5m_tokens) AS cw5, SUM(cache_write_1h_tokens) AS cw1, "
        "SUM(output_tokens) AS output "
        f"FROM usage_events WHERE {' AND '.join(where)} GROUP BY model"  # sql-audited
    )

    catalog = load_catalog()
    totals = {"fresh": 0, "cache_read": 0, "cw5": 0, "cw1": 0}
    actual = 0.0
    uncached = 0.0
    complete = True

    for row in store.query(sql, tuple(params)):
        for key in totals:
            totals[key] += row[key] or 0
        price = catalog.get(row["model"])
        cached = row["cache_read"] or 0
        if price is None:
            if cached:
                complete = False
            continue

        fresh, cw5, cw1, out = (
            row["fresh"] or 0,
            row["cw5"] or 0,
            row["cw1"] or 0,
            row["output"] or 0,
        )
        cw1_rate = (
            price.cache_write_1h if price.cache_write_1h is not None else price.cache_write_5m
        )
        actual += (
            fresh * price.input
            + cached * price.cache_read
            + cw5 * price.cache_write_5m
            + cw1 * (cw1_rate or 0.0)
            + out * price.output
        ) / 1_000_000
        # Every input-side token at the full rate — no cache reads, no writes.
        uncached += ((fresh + cached + cw1 + cw5) * price.input + out * price.output) / 1_000_000

    # The net saving, so the cost of writing to the cache is subtracted rather
    # than ignored.
    savings = uncached - actual

    return CacheEfficiency(
        cache_read=totals["cache_read"],
        cache_write_5m=totals["cw5"],
        cache_write_1h=totals["cw1"],
        fresh_input=totals["fresh"],
        savings_usd=savings if complete else None,
        actual_usd=actual if complete else None,
        without_cache_usd=uncached if complete else None,
    )
