"""Incremental scanner: walk every adapter's sources and persist what is new.

The scan is designed to be run often and cheaply — a menu bar app polls it every
few seconds. Three properties make that safe:

* **Incremental.** Each file records a byte offset, so a rescan reads only what
  was appended. A full cold scan of ~18 MB takes about 40 ms; steady state reads
  a few KB.
* **Idempotent.** Dedup lives in the ``usage_events`` primary key, so rescanning
  unchanged data inserts nothing. Correctness does not depend on the offset
  bookkeeping being perfect — only its speed does.
* **Fault-isolated.** One unreadable or hostile file fails that file, not the
  scan. Errors are collected already-redacted and reported in aggregate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .adapters import Adapter, get_adapters
from .config import Config, load_config
from .models import CostBasis
from .pricing import Catalog, load_catalog, price_events
from .pricing.calculator import compute_cost, resolve_basis
from .safety import AdapterError, SecurityError, path_key, redact_path
from .store import ScanState, Store

__all__ = ["ScanReport", "scan"]


@dataclass(slots=True)
class ScanReport:
    """What a scan did. Every field is safe to print or paste into an issue."""

    files_seen: int = 0
    files_parsed: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    lines_read: int = 0
    lines_skipped: int = 0
    events_found: int = 0
    events_new: int = 0
    duplicates_dropped: int = 0
    """Repeat records collapsed during parsing — the overcount avoided."""
    integrity_checks: int = 0
    """Self-consistency checks a provider's format allowed us to run."""

    integrity_failures: int = 0
    """Checks that did not reconcile. Any non-zero value means our parse
    disagrees with the provider's own totals, and the number reported is not
    trustworthy — so it is shown loudly rather than averaged away."""

    events_unpriced: int = 0
    """New events for which no price is known. Stored with cost NULL, shown as
    '—', and never counted as zero."""
    unpriced_models: set[str] = field(default_factory=set)
    quotas_new: int = 0
    duration_s: float = 0.0
    pruned: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def events_already_known(self) -> int:
        """Parsed events that were already in the store (a re-read of old data)."""
        return self.events_found - self.events_new

    @property
    def records_seen(self) -> int:
        """Usage records read, before duplicates were collapsed."""
        return self.events_found + self.duplicates_dropped


def scan(
    store: Store,
    *,
    adapters: list[Adapter] | None = None,
    config: Config | None = None,
    catalog: Catalog | None = None,
    force: bool = False,
) -> ScanReport:
    """Scan all sources into ``store``.

    ``force`` re-reads every file from byte zero, discarding saved offsets. It
    exists for when the parser itself changes — old offsets would then skip
    records the new code would have handled differently.
    """
    started = time.perf_counter()
    cfg = config or load_config()
    cat = catalog if catalog is not None else load_catalog()
    report = ScanReport()

    for adapter in adapters if adapters is not None else get_adapters():
        if not getattr(adapter, "implemented", True):
            # Knows where its logs are but cannot read them yet. Skipping here
            # keeps `doctor` able to report the provider as detected without the
            # scanner pretending to have processed it.
            continue
        for source in adapter.sources():
            for file_path in source.discover():
                report.files_seen += 1
                try:
                    _scan_one(store, adapter, source.root, file_path, cfg, cat, force, report)
                except (AdapterError, SecurityError, OSError) as exc:
                    report.files_failed += 1
                    report.errors.append(f"{adapter.name}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    # A bug in one adapter must not abort every other provider's
                    # scan. Only the type name is kept: the exception text could
                    # quote a line of transcript.
                    report.files_failed += 1
                    report.errors.append(
                        f"{adapter.name}: unexpected {type(exc).__name__} in "
                        f"{redact_path(file_path)}"
                    )

    # Bound the database on the way out. Cheap: an indexed delete over a range
    # that is usually empty.
    report.pruned = _prune(store, cfg)

    report.duration_s = time.perf_counter() - started
    return report


def _prune(store: Store, cfg: Config) -> dict[str, int]:
    now = datetime.now(UTC)
    quota_before = (
        _iso_days_ago(now, cfg.retention.quota_days) if cfg.retention.quota_days else None
    )
    events_before = (
        _iso_days_ago(now, cfg.retention.events_days) if cfg.retention.events_days else None
    )
    if not quota_before and not events_before:
        return {}
    return store.prune(quota_before=quota_before, events_before=events_before)


def _iso_days_ago(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scan_one(
    store: Store,
    adapter: Adapter,
    root: Path,
    file_path: Path,
    cfg: Config,
    catalog: Catalog,
    force: bool,
    report: ScanReport,
) -> None:
    key = path_key(file_path)
    st = file_path.stat()
    state = None if force else store.get_scan_state(key)

    if state is not None:
        if state.is_stale_for(st):
            # Rotated or truncated: the offset now means something else.
            offset = 0
        elif st.st_size == state.size and st.st_mtime_ns == state.mtime_ns:
            # Unchanged bytes usually mean nothing to do. The exception is a
            # source whose output is *derived* from the whole series rather than
            # read off a line — Claude's window reset time, for instance, which
            # only becomes computable once enough history exists. Those are
            # re-read so an upgrade does not have to wait for the file to change.
            if not getattr(adapter, "rescan_unchanged", False):
                report.files_unchanged += 1
                return
            offset = 0
        else:
            offset = state.offset
    else:
        offset = 0

    result = adapter.parse(file_path, root, offset=offset, project_mode=cfg.privacy.project_paths)

    # Price before storing. Costs are recomputed on demand by `reprice` when
    # rates change, so this is a cache of the current best answer, not a
    # permanent verdict.
    subscription = cfg.billing.subscription_for(adapter.name, detected=_detect_subscription(result))
    priced = price_events(result.events, catalog, subscription=subscription)

    for event in priced:
        if event.cost_usd is None:
            report.events_unpriced += 1
            report.unpriced_models.add(event.model)

    report.files_parsed += 1
    report.lines_read += result.lines_read
    report.lines_skipped += result.lines_skipped
    report.events_found += len(priced)
    report.duplicates_dropped += result.duplicates_dropped
    report.integrity_checks += result.integrity_checks
    report.integrity_failures += result.integrity_failures
    report.events_new += store.upsert_events(priced)
    report.quotas_new += store.record_quota(result.quotas)

    store.set_scan_state(
        ScanState(
            path_key=key,
            # Bare filename only — a session UUID, which identifies nothing.
            path_label=file_path.name,
            inode=st.st_ino,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            offset=result.offset,
            last_scan=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )


def _detect_subscription(result) -> bool | None:
    """Infer a subscription from what the provider itself recorded in this file.

    Codex writes ``plan_type`` alongside its rate limits; its presence is proof
    the user is on a ChatGPT plan rather than paying per token.
    """
    for quota in result.quotas:
        if quota.plan_type:
            return True
    return None


def detect_subscription_in_store(store: Store, provider: str) -> bool | None:
    """Look for standing evidence of a subscription, across everything scanned.

    Claude Code's own transcripts say nothing about how the account is billed,
    which used to leave every user labelled ``api_equivalent`` by assumption —
    right for most, but wrong for anyone paying per token, who would see their
    real bill dressed up as a hypothetical.

    Two pieces of evidence settle it without touching a credential:

    * Claude's desktop app records plan *utilisation* percentages. Those exist
      only for an account on a plan, so a recent reading is proof of one.
    * Codex records ``plan_type`` beside its rate limits.

    ``None`` when nothing is known, which still falls back to the safe default.
    """
    if provider == "codex":
        rows = store.latest_quota("codex")
        if any(row["plan_type"] for row in rows):
            return True
        return None

    if provider == "claude_code":
        # Utilisation readings only exist for an account on a plan.
        if list(store.latest_quota("claude")):
            return True
        return None

    return None


def describe_errors(report: ScanReport, limit: int = 5) -> list[str]:
    """First few errors, already redacted by construction."""
    return report.errors[:limit]


def reprice(
    store: Store,
    *,
    config: Config | None = None,
    catalog: Catalog | None = None,
) -> tuple[int, int]:
    """Recompute every stored cost against the current catalog.

    Needed whenever prices move, the overlay gains a model, or the user edits
    ``pricing.toml`` — otherwise a model that was unpriced at scan time would
    stay ``—`` forever even after we learn its rate.

    Returns ``(updated, still_unpriced)``.
    """
    cfg = config or load_config()
    cat = catalog if catalog is not None else load_catalog()

    updates: list[tuple[str, float | None, CostBasis, str | None]] = []
    unpriced = 0

    for key, provider, model, tokens in store.iter_priceable():
        price = cat.get(model)
        if price is None:
            unpriced += 1
            updates.append((key, None, CostBasis.UNPRICED, f"no price for {model!r}"))
            continue
        usd, note = compute_cost(tokens, price)
        subscription = cfg.billing.subscription_for(provider)
        updates.append((key, usd, resolve_basis(provider, subscription=subscription), note))

    return store.update_costs(updates), unpriced
