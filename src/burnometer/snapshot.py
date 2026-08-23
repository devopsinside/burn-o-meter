"""The UI payload: everything a menu bar needs, computed once in Python.

The macOS app reads this file rather than querying SQLite itself. That decision
buys three things:

* **The UI cannot disagree with the CLI.** Both read the same numbers from
  :mod:`burnometer.analytics`; there is no second implementation of the
  aggregation rules in another language to drift out of sync.
* **No read-only WAL problem.** macOS's system SQLite will not open a
  WAL-mode database on a read-only connection (it cannot create the ``-shm``),
  while Python's bundled build will. Reading JSON sidesteps that entirely
  without weakening the read-only guarantee by opening the store read-write.
* **A shell for any platform is cheap.** A Windows or Linux tray app needs to
  parse one small JSON file, not reimplement SQL.

The file is written whenever a scan runs, so its freshness is exactly the scan
interval. ``generated_at`` is included so a UI can say how stale it is rather
than presenting old numbers as current.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .analytics import aggregate, blocks, cache_efficiency, time_series
from .config import burn_home
from .models import CostBasis
from .safety import harden_path, secure_dir, secure_open_write
from .store import Store

__all__ = [
    "SNAPSHOT_SCHEMA",
    "snapshot_path",
    "engine_path",
    "build_snapshot",
    "write_snapshot",
    "write_engine_pointer",
]

SNAPSHOT_SCHEMA = 2
SPARKLINE_DAYS = 14
TOP_MODELS = 6


def snapshot_path() -> Path:
    return burn_home() / "snapshot.json"


def engine_path() -> Path:
    """Where the executable's location is recorded for the UI to find.

    Kept separate from ``snapshot.json`` on purpose. The snapshot is the file a
    user is most likely to hand to someone while reporting a bug, so it stays
    free of filesystem paths; this one exists only so a GUI app — which launches
    with a minimal PATH and cannot otherwise find a venv or pipx install — can
    invoke the engine to refresh itself.
    """
    return burn_home() / "engine.json"


# Directories whose paths survive an upgrade. Homebrew installs into
# ``/opt/homebrew/Cellar/<formula>/<version>/`` and symlinks from ``bin``, so
# recording the Cellar path bakes in a version number that is wrong the moment the
# user runs ``brew upgrade`` -- and the app cannot re-derive it, because only the
# engine writes this file. Preferring the symlink keeps the pointer valid.
_STABLE_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".local/bin"),
)


def _stable_path(executable: str) -> str:
    """Return a path to the same binary that will still be valid after an upgrade."""
    try:
        real = Path(executable).resolve()
    except OSError:
        return executable
    for directory in _STABLE_BIN_DIRS:
        candidate = Path(directory) / Path(executable).name
        try:
            if candidate.exists() and candidate.resolve() == real:
                return str(candidate)
        except OSError:
            continue
    return executable


def _window_already_reset(resets_at: str | None, observed_at: str | None) -> bool:
    """True when the window a reading describes has rolled over since it was taken.

    Both timestamps come from the provider's own logs, so an unparseable one means
    we cannot establish this - and we keep the reading rather than silently drop a
    figure the user may be relying on.
    """
    if not resets_at or not observed_at:
        return False
    try:
        resets = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return resets <= observed or resets <= datetime.now(UTC)


def write_engine_pointer(path: Path | None = None) -> Path:
    """Record how to invoke this engine, so the menu bar can trigger a scan."""
    import json as _json
    import sys

    target = path or engine_path()
    secure_dir(target.parent)

    executable = shutil.which("burnometer")
    if executable:
        argv = [_stable_path(executable)]
    else:
        candidate = Path(sys.executable).parent / "burnometer"
        argv = [str(candidate)] if candidate.exists() else [sys.executable, "-m", "burnometer"]

    with secure_open_write(target) as fh:
        fh.write(_json.dumps({"argv": argv}, indent=1).encode("utf-8"))
    harden_path(target)
    return target


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def build_snapshot(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Assemble the payload. Contains only aggregates — never prompt content."""
    now = now or datetime.now(UTC)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today = aggregate(store, "model", since=start_of_day)
    window_report = blocks(store)
    current = window_report.current

    daily = aggregate(store, "day", since=now - timedelta(days=SPARKLINE_DAYS))
    # Oldest first, so a UI can draw it left to right without re-sorting.
    sparkline = sorted(
        ({"day": row.key, "cost_usd": _money(row.totals.cost_usd or 0.0)} for row in daily.rows),
        key=lambda item: item["day"],
    )
    ranges = _build_ranges(store, now)

    rows = [
        {
            "model": row.key,
            "provider": row.provider,
            "cost_basis": row.basis.value,
            "requests": row.totals.requests,
            "cost_usd": _money(row.totals.cost_usd),
            "total_tokens": row.totals.tokens.total,
            "cache_hit_rate": row.totals.cache_hit_rate,
            "effective_rate_usd_per_mtok": row.totals.effective_rate,
            "price_source": row.price_source,
            # Where this model's spend went. Precomputed so a UI can reveal it
            # without querying — the app renders numbers, it never derives them.
            "projects": _projects_for(store, row.key, start_of_day),
        }
        for row in today.rows[:TOP_MODELS]
    ]

    quotas = []
    for provider in ("claude", "codex", "claude_code"):
        for q in store.latest_quota(provider):
            observed = q["observed_at"]
            age = None
            if observed:
                try:
                    age = int(
                        (
                            now - datetime.fromisoformat(observed.replace("Z", "+00:00"))
                        ).total_seconds()
                    )
                except ValueError:
                    age = None
            # A reading taken before its own window reset describes a window that
            # no longer exists. Showing "0% used" from a window that has since
            # rolled over is not stale data, it is wrong data - the current window
            # could be at any figure at all, and we have no observation of it.
            if _window_already_reset(q["resets_at"], observed):
                continue

            quotas.append(
                {
                    "provider": q["provider"],
                    "window": q["window_name"],
                    "used_percent": q["used_percent"],
                    "window_minutes": q["window_minutes"],
                    "resets_at": q["resets_at"],
                    "plan_type": q["plan_type"],
                    # 'exact' means the provider reported it. Anything else is
                    # derived by us and every surface must say so.
                    "exact": q["source"] == "exact",
                    "observed_at": observed,
                    # Claude's figures are only written while the desktop app is
                    # running, so a reading can be hours old. The UI needs the
                    # age to avoid presenting one as current.
                    "age_seconds": age,
                }
            )

    window: dict[str, Any] | None = None
    if current is not None:
        window = {
            "start": current.start.isoformat(),
            "expires_at": current.expires_at.isoformat(),
            "remaining_seconds": int(current.remaining(now).total_seconds()),
            "requests": current.requests,
            "cost_usd": _money(current.cost_usd),
            "cost_basis": current.basis.value,
            "relative_to_median": window_report.relative_to_history(current),
            # No percentage: Anthropic publishes no token limit for subscription
            # plans, so one could only be invented.
            "has_published_limit": False,
        }

    stats = store.stats()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "last_scan_at": stats.get("latest"),
        "today": {
            "rows": rows,
            # Keyed by basis, with no combined total — the UI inherits the rule
            # rather than having to know about it.
            "subtotals": {
                basis.value: {
                    "cost_usd": _money(totals.cost_usd),
                    "requests": totals.requests,
                }
                for basis, totals in today.subtotals.items()
            },
            "cost_basis_notes": {
                basis.value: _BASIS_NOTES[basis]
                for basis in today.subtotals
                if basis in _BASIS_NOTES
            },
            "unpriced_models": sorted(today.unpriced_models),
        },
        "sparkline": sparkline,
        "ranges": ranges,
        "current_window": window,
        "quotas": quotas,
        "totals": {"events": stats.get("events", 0), "providers": stats.get("providers", [])},
    }


#: Chart ranges offered in the UI. Each is a label, a bucket size, and how far
#: back it starts. Hourly for a single day; daily beyond that, because 720
#: hourly bars in a popover is noise rather than detail.
RANGE_SPECS = (
    ("today", "Today", "hour"),
    ("month", "This month", "day"),
    ("days30", "30 days", "day"),
)


#: Projects shown when a model row is expanded. Enough to see where the money
#: went without turning a popover into a table.
TOP_PROJECTS = 5


def _projects_for(store: Store, model: str, since: datetime) -> list[dict[str, Any]]:
    """Per-project breakdown for one model.

    Shares are computed within the model rather than against the day's total, so
    an expanded row reads as "of this model's spend" — which is the question the
    reader just asked by expanding it.
    """
    report = aggregate(store, "project", since=since, model=model)
    total = sum((totals.cost_usd or 0.0) for totals in report.subtotals.values())
    out: list[dict[str, Any]] = []
    for row in report.rows[:TOP_PROJECTS]:
        cost = row.totals.cost_usd
        out.append(
            {
                "project": row.key,
                "requests": row.totals.requests,
                "cost_usd": _money(cost),
                "share_of_model": (cost / total) if (cost is not None and total) else None,
                "cache_hit_rate": row.totals.cache_hit_rate,
            }
        )
    return out


def _range_start(key: str, now: datetime) -> datetime:
    if key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)


def _build_ranges(store: Store, now: datetime) -> list[dict[str, Any]]:
    """One chart series per selectable range, each with its own totals.

    Totals are recomputed per range rather than summed from the chart, so a
    range's headline figure obeys the same basis rules as everywhere else
    instead of being a chart artefact.
    """
    out: list[dict[str, Any]] = []
    for key, label, bucket in RANGE_SPECS:
        start = _range_start(key, now)
        series = time_series(store, bucket=bucket, since=start)
        report = aggregate(store, "model", since=start)
        cache = cache_efficiency(store, since=start)
        out.append(
            {
                "key": key,
                "label": label,
                "bucket": bucket,
                "providers": series.providers,
                "points": [
                    {
                        "label": point.label,
                        "by_provider": {
                            provider: _money(cost) for provider, cost in point.by_provider.items()
                        },
                        "total": _money(point.total),
                    }
                    for point in series.points
                ],
                "peak": _money(series.peak),
                "subtotals": {
                    basis.value: {
                        "cost_usd": _money(totals.cost_usd),
                        "requests": totals.requests,
                    }
                    for basis, totals in report.subtotals.items()
                },
                "cache": {
                    "hit_rate": cache.hit_rate,
                    "cache_read": cache.cache_read,
                    "fresh_input": cache.fresh_input,
                    "cache_write": cache.cache_write_5m + cache.cache_write_1h,
                    "savings_usd": _money(cache.savings_usd),
                    "actual_usd": _money(cache.actual_usd),
                    "without_cache_usd": _money(cache.without_cache_usd),
                    "output_tokens": None,
                },
            }
        )
    return out


_BASIS_NOTES = {
    CostBasis.API_BILLED: "billed per token against an API key",
    CostBasis.API_EQUIVALENT: "subscription — not billed per token; API-equivalent value",
    CostBasis.UNPRICED: "no published rate for these models",
    CostBasis.NOT_METERED: "not billed per token — self-hosted, or covered by a plan",
}


def write_snapshot(store: Store, path: Path | None = None) -> Path:
    """Write the payload atomically, owner-readable only."""
    target = path or snapshot_path()
    secure_dir(target.parent)
    payload = build_snapshot(store)

    # Write to a temp file and rename, so a UI polling this never sees a
    # half-written document.
    temp = target.with_suffix(".json.tmp")
    with secure_open_write(temp) as fh:
        fh.write(json.dumps(payload, indent=1, sort_keys=False).encode("utf-8"))
    temp.replace(target)
    harden_path(target)
    write_engine_pointer()
    return target
