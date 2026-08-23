"""Core data contract shared by every adapter, the pricing engine and the store.

The single most important thing in this module is the set of invariants on
:class:`TokenCounts`. Providers report token usage in mutually incompatible
shapes, and the common accounting bugs in this problem space all come from
mixing them up:

* Claude Code reports ``input_tokens`` already excluding cache reads.
* Codex reports ``input_tokens`` *including* ``cached_input_tokens``, so adding
  the two double-counts every cached token.
* Codex reports ``reasoning_output_tokens`` as a subset of ``output_tokens``,
  so adding the two double-counts every reasoning token.

Adapters are responsible for normalising into the shape documented below.
Everything downstream may then assume it without re-checking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

__all__ = [
    "CostBasis",
    "QuotaSource",
    "TokenCounts",
    "UsageEvent",
    "QuotaSnapshot",
    "model_family",
]


class CostBasis(StrEnum):
    """How a dollar figure should be interpreted. Never guess between these."""

    API_BILLED = "api_billed"
    """Real money. The user pays per token against an API key."""

    API_EQUIVALENT = "api_equivalent"
    """Counterfactual. The user is on a subscription and is NOT billed per
    token; this is what the same usage would have cost at API rates. Always
    render with a leading '~' and an explanatory note."""

    UNPRICED = "unpriced"
    """No price is known for this model. Cost is NULL, never 0.0."""

    NOT_METERED = "not_metered"
    """These tokens are not billed per token at all, so no amount of money is
    the right answer — not even zero.

    Two cases produce it. A model run locally (Ollama, LM Studio, llama.cpp)
    costs electricity and hardware, not tokens. And a plan-included model —
    Kimi's coding plans, for instance — publishes a rate of 0 because usage is
    covered by a subscription, which is a very different claim from "free".

    Distinct from UNPRICED on purpose: that means *we do not know the rate*,
    this means *there is no rate*. Showing $0.00 for either would tell a user
    their work was free."""


class QuotaSource(StrEnum):
    """Whether a quota reading came from the provider or was derived by us."""

    EXACT = "exact"
    """Reported by the provider itself (e.g. Codex writes rate_limits to disk)."""

    ESTIMATED = "estimated"
    """Derived locally from timestamps. Must be labelled as an estimate in
    every surface that displays it."""


@dataclass(slots=True, frozen=True)
class TokenCounts:
    """Normalised token counts for a single billable request.

    Invariants that adapters MUST establish:

    ``input``
        Uncached input tokens **only**. Tokens served from cache belong in
        ``cache_read`` and must not also appear here.
    ``reasoning``
        A display-only **subset** of ``output``. Never add it to a total; it is
        already inside ``output``.
    ``cache_write_5m`` / ``cache_write_1h``
        Split by cache TTL, because they are billed at different multipliers
        (1.25x base input for 5-minute, 2.0x for 1-hour). Together they equal
        the provider's single cache-creation figure.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def cache_write(self) -> int:
        """Total cache-creation tokens across both TTLs."""
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        """Every distinctly-billed token.

        ``reasoning`` is deliberately excluded: it is a subset of ``output``
        and including it would double-count.
        """
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: TokenCounts) -> TokenCounts:
        if not isinstance(other, TokenCounts):
            return NotImplemented
        return TokenCounts(
            input=self.input + other.input,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
            cache_read=self.cache_read + other.cache_read,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
        )

    def __radd__(self, other: TokenCounts | int) -> TokenCounts:
        # sum() seeds with int 0; treat that as the identity element.
        if other == 0:
            return self
        return self.__add__(other)  # type: ignore[arg-type]

    def __bool__(self) -> bool:
        return self.total > 0


_CLAUDE_FAMILY = re.compile(r"^(claude-[a-z]+)")
_OPENAI_FAMILY = re.compile(r"^(gpt-\d+)")
_OPENAI_TIER = re.compile(r"-(mini|nano|pro|codex)\b")


def model_family(slug: str) -> str:
    """Best-effort grouping label for a raw provider model slug.

    This is *only* a grouping convenience for per-model reports. The raw slug is
    always stored alongside and is what any cost calculation uses — we never
    normalise away an identity the provider actually reported, because
    ``gpt-5.6-terra`` and ``gpt-5.5`` really are different models with different
    prices even though both group under ``gpt-5``.

    Unrecognised slugs return themselves, so a new model shows up as its own
    group rather than being silently folded into an unrelated one.
    """
    if not slug:
        return "unknown"
    s = slug.strip().lower()

    if m := _CLAUDE_FAMILY.match(s):
        return m.group(1)

    if m := _OPENAI_FAMILY.match(s):
        base = m.group(1)
        tier = _OPENAI_TIER.search(s)
        return f"{base}-{tier.group(1)}" if tier else base

    return s


@dataclass(slots=True)
class UsageEvent:
    """One billable request, normalised across providers.

    ``event_key`` is the global dedup key and must be stable across rescans.
    Claude Code writes the same assistant message to its transcript several
    times over (verified: up to 7 copies, byte-identical usage payloads), so
    this key is what stops a 2.5x overcount.
    """

    event_key: str
    provider: str
    model: str
    ts: datetime
    tokens: TokenCounts = field(default_factory=TokenCounts)

    model_family: str = ""
    """Grouping label. Derived from ``model`` when left empty."""

    effort: str | None = None
    """Codex reasoning effort (``low``..``max``). ``None`` for Claude Code.

    A sub-dimension of the model, not part of its identity: effort changes how
    many tokens are consumed but not the per-token rate, so it rolls up under
    the model by default.
    """

    session_id: str | None = None
    project: str | None = None
    git_branch: str | None = None

    # Populated by the pricing engine, not by adapters.
    cost_usd: float | None = None
    cost_basis: CostBasis = CostBasis.UNPRICED
    price_source: str | None = None

    # Provenance, so any number can be traced back to a line on disk.
    raw_file: str | None = None
    raw_line: int | None = None

    def __post_init__(self) -> None:
        if not self.model_family:
            self.model_family = model_family(self.model)

    def priced(
        self,
        cost_usd: float | None,
        basis: CostBasis,
        source: str | None,
    ) -> UsageEvent:
        """Return a copy carrying a pricing decision."""
        return replace(self, cost_usd=cost_usd, cost_basis=basis, price_source=source)


@dataclass(slots=True)
class QuotaSnapshot:
    """A point-in-time reading of how much of a rate-limit window is consumed.

    Codex writes these to its own logs, so they are ``EXACT``. Claude Code does
    not persist quota anywhere on disk, so its readings are ``ESTIMATED`` from
    local timestamps until the user opts into an authenticated lookup.
    """

    provider: str
    window_name: str
    used_percent: float | None
    observed_at: datetime
    source: QuotaSource

    window_minutes: int | None = None
    resets_at: datetime | None = None
    plan_type: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.source is QuotaSource.EXACT
