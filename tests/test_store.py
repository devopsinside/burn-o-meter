"""Store behaviour: permissions, idempotence, and the schema's honesty checks."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from burnometer.models import CostBasis, QuotaSnapshot, QuotaSource, TokenCounts, UsageEvent
from burnometer.store import Store


def _event(key: str = "k1", **kw) -> UsageEvent:
    defaults = dict(
        event_key=key,
        provider="claude_code",
        model="claude-opus-5",
        ts=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        tokens=TokenCounts(input=10, output=20, cache_read=100, cache_write_1h=50),
        cost_usd=0.001,
        cost_basis=CostBasis.API_BILLED,
        price_source="test",
    )
    return UsageEvent(**{**defaults, **kw})


def test_schema_version_and_tables(store: Store) -> None:
    assert store.schema_version == 1
    assert store.count_events() == 0


def test_database_and_dir_permissions(burn_home: Path) -> None:
    """G4: never a world-readable window on the DB or its sidecars."""
    with Store.open(burn_home / "burn.db"):
        assert stat.S_IMODE(burn_home.stat().st_mode) == 0o700, "home dir must be 0700"
        for suffix in ("", "-wal"):
            p = Path(str(burn_home / "burn.db") + suffix)
            if p.exists():
                assert stat.S_IMODE(p.stat().st_mode) == 0o600, f"{p.name} must be 0600"


def test_insert_is_idempotent(store: Store) -> None:
    """Rescanning unchanged files must add nothing. This is what stops the 2.5x
    overcount that naive summation of Claude Code transcripts produces."""
    assert store.upsert_events([_event("a"), _event("b")]) == 2
    assert store.upsert_events([_event("a"), _event("b")]) == 0
    assert store.count_events() == 2


def test_model_family_derived(store: Store) -> None:
    store.upsert_events([_event("a", model="claude-opus-4-8")])
    row = store._conn.execute("SELECT model, model_family FROM usage_events").fetchone()
    assert row["model"] == "claude-opus-4-8", "raw slug preserved"
    assert row["model_family"] == "claude-opus"


def test_unpriced_must_have_null_cost(store: Store) -> None:
    """'Free' and 'unknown' are different facts; the schema refuses to conflate them."""
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_events([_event("bad", cost_basis=CostBasis.UNPRICED, cost_usd=0.0)])


def test_priced_must_have_a_cost(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_events([_event("bad", cost_basis=CostBasis.API_BILLED, cost_usd=None)])


def test_unpriced_roundtrip(store: Store) -> None:
    store.upsert_events([_event("u", cost_basis=CostBasis.UNPRICED, cost_usd=None)])
    assert store.stats()["unpriced"] == 1


def test_read_only_connection_rejects_writes(burn_home: Path) -> None:
    """G4: the menu bar app opens this same file. A UI bug must not be able to
    corrupt it — enforced by the driver, not by convention."""
    db = burn_home / "burn.db"
    with Store.open(db) as w:
        w.upsert_events([_event("a")])

    with Store.open(db, read_only=True) as ro:
        assert ro.count_events() == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.upsert_events([_event("b")])


def test_quota_snapshot_roundtrip(store: Store) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    store.record_quota(
        [
            QuotaSnapshot(
                provider="codex",
                window_name="primary",
                used_percent=52.0,
                observed_at=now,
                source=QuotaSource.EXACT,
                window_minutes=10080,
                plan_type="go",
            )
        ]
    )
    rows = store.latest_quota("codex")
    assert len(rows) == 1
    assert rows[0]["used_percent"] == 52.0
    assert rows[0]["source"] == "exact"


def test_scan_state_roundtrip_and_staleness(store: Store) -> None:
    from burnometer.store.db import ScanState

    store.set_scan_state(
        ScanState(path_key="a" * 32, path_label="a.jsonl", inode=1, size=500, offset=500)
    )
    got = store.get_scan_state("a" * 32)
    assert got is not None and got.offset == 500
    assert got.path_label == "a.jsonl"

    class FakeStat:
        st_ino, st_size = 1, 400  # truncated below our offset

    assert got.is_stale_for(FakeStat()) is True

    class Grown:
        st_ino, st_size = 1, 900  # normal incremental growth

    assert got.is_stale_for(Grown()) is False

    class Rotated:
        st_ino, st_size = 2, 900

    assert got.is_stale_for(Rotated()) is True


def test_prune_bounds_quota_history(store: Store) -> None:
    """Codex emits a quota reading per turn and Claude's desktop app writes one
    every 15 minutes, so readings accumulate far faster than usage."""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    new = datetime(2026, 8, 21, tzinfo=UTC)
    store.record_quota(
        [
            QuotaSnapshot(
                provider="codex",
                window_name="primary",
                used_percent=10.0,
                observed_at=old,
                source=QuotaSource.EXACT,
            ),
            QuotaSnapshot(
                provider="codex",
                window_name="primary",
                used_percent=20.0,
                observed_at=new,
                source=QuotaSource.EXACT,
            ),
        ]
    )
    removed = store.prune(quota_before="2026-01-01T00:00:00.000Z")
    assert removed["quota_snapshots"] == 1
    rows = store.latest_quota("codex")
    assert len(rows) == 1 and rows[0]["used_percent"] == 20.0


def test_prune_leaves_usage_events_alone_by_default(store: Store) -> None:
    """Usage is the record of what was spent; retention defaults to keeping it."""
    store.upsert_events([_event("a", ts=datetime(2020, 1, 1, tzinfo=UTC))])
    removed = store.prune(quota_before="2026-01-01T00:00:00.000Z")
    assert removed.get("usage_events", 0) == 0
    assert store.count_events() == 1


def test_prune_can_drop_old_events_when_asked(store: Store) -> None:
    store.upsert_events(
        [
            _event("old", ts=datetime(2020, 1, 1, tzinfo=UTC)),
            _event("new", ts=datetime(2026, 8, 21, tzinfo=UTC)),
        ]
    )
    removed = store.prune(events_before="2026-01-01T00:00:00.000Z")
    assert removed["usage_events"] == 1
    assert store.count_events() == 1


def test_not_metered_must_have_null_cost(store: Store) -> None:
    """The schema refuses to let self-hosted or plan-included usage claim a
    dollar figure — including zero."""
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_events([_event("x", cost_basis=CostBasis.NOT_METERED, cost_usd=0.0)])


def test_not_metered_stores_with_null_cost(store: Store) -> None:
    store.upsert_events([_event("x", cost_basis=CostBasis.NOT_METERED, cost_usd=None)])
    assert store.count_events() == 1
