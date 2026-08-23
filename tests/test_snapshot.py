"""The UI payload: shape, permissions, and the honesty rules it must carry.

The menu bar renders this file and computes nothing itself, so any rule the CLI
enforces has to survive the trip through JSON or the two surfaces will disagree.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from burnometer.models import CostBasis, QuotaSnapshot, QuotaSource, TokenCounts, UsageEvent
from burnometer.snapshot import SNAPSHOT_SCHEMA, build_snapshot, snapshot_path, write_snapshot
from burnometer.store import Store

NOW = datetime.now(UTC)


def _ev(
    key: str,
    *,
    model="claude-opus-5",
    cost=1.0,
    basis=CostBasis.API_BILLED,
    minutes=0,
    provider="claude_code",
    tokens=None,
) -> UsageEvent:
    return UsageEvent(
        event_key=key,
        provider=provider,
        model=model,
        ts=NOW - timedelta(minutes=minutes),
        tokens=tokens or TokenCounts(input=100, output=200, cache_read=700, cache_write_1h=50),
        project="demo",
        cost_usd=cost,
        cost_basis=basis,
        price_source="test",
    )


def test_snapshot_has_everything_the_ui_needs(store: Store) -> None:
    store.upsert_events([_ev("a"), _ev("b", minutes=5)])
    payload = build_snapshot(store)
    for key in ("schema", "generated_at", "today", "sparkline", "current_window", "quotas"):
        assert key in payload, f"missing {key}"
    assert payload["schema"] == SNAPSHOT_SCHEMA
    assert payload["today"]["rows"][0]["model"] == "claude-opus-5"


def test_subtotals_stay_keyed_by_basis(store: Store) -> None:
    """The no-fusing rule must survive serialisation, or the UI reinvents it."""
    store.upsert_events(
        [
            _ev("a", cost=10.0, basis=CostBasis.API_BILLED),
            _ev("b", cost=5.0, basis=CostBasis.API_EQUIVALENT, minutes=1),
        ]
    )
    today = build_snapshot(store)["today"]
    assert set(today["subtotals"]) == {"api_billed", "api_equivalent"}
    assert today["subtotals"]["api_billed"]["cost_usd"] == 10.0
    assert "total_cost_usd" not in today, "no fused total may be exposed to the UI"


def test_each_basis_carries_its_explanation(store: Store) -> None:
    """A dollar figure must never reach a screen without saying what kind."""
    store.upsert_events([_ev("a", basis=CostBasis.API_EQUIVALENT)])
    notes = build_snapshot(store)["today"]["cost_basis_notes"]
    assert "subscription" in notes["api_equivalent"]


def test_unpriced_is_null_and_listed(store: Store) -> None:
    store.upsert_events(
        [
            _ev("a", cost=2.0),
            _ev("u", model="mystery", cost=None, basis=CostBasis.UNPRICED, minutes=1),
        ]
    )
    today = build_snapshot(store)["today"]
    row = next(r for r in today["rows"] if r["model"] == "mystery")
    assert row["cost_usd"] is None, "0.0 would claim it was free"
    assert row["total_tokens"] > 0
    assert "mystery" in today["unpriced_models"]


def test_window_states_that_no_limit_is_published(store: Store) -> None:
    """The absence of a percentage must read as a decision, not an oversight."""
    store.upsert_events([_ev("a")])
    window = build_snapshot(store)["current_window"]
    assert window is not None
    assert window["has_published_limit"] is False
    assert "used_percent" not in window
    assert window["remaining_seconds"] > 0


def test_quota_marks_provider_reported_readings_as_exact(store: Store) -> None:
    store.upsert_events([_ev("a", provider="codex", model="gpt-5.5")])
    store.record_quota(
        [
            QuotaSnapshot(
                provider="codex",
                window_name="primary",
                used_percent=44.0,
                observed_at=NOW,
                source=QuotaSource.EXACT,
                window_minutes=10080,
                plan_type="go",
            )
        ]
    )
    quota = build_snapshot(store)["quotas"][0]
    assert quota["exact"] is True
    assert quota["used_percent"] == 44.0


def test_price_source_travels_with_every_row(store: Store) -> None:
    """Provenance has to reach the UI, or the menu bar shows unattributable money."""
    store.upsert_events([_ev("a")])
    assert build_snapshot(store)["today"]["rows"][0]["price_source"] == "test"


def test_sparkline_is_oldest_first(store: Store) -> None:
    store.upsert_events(
        [
            _ev("a", minutes=60 * 24 * 3),
            _ev("b", minutes=60 * 24 * 1),
            _ev("c", minutes=5),
        ]
    )
    days = [row["day"] for row in build_snapshot(store)["sparkline"]]
    assert days == sorted(days), "a UI must be able to draw it left to right as-is"


def test_written_file_is_owner_only_and_valid_json(store: Store, burn_home: Path) -> None:
    store.upsert_events([_ev("a")])
    path = write_snapshot(store)
    assert path == snapshot_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["schema"] == SNAPSHOT_SCHEMA


def test_write_is_atomic(store: Store, burn_home: Path) -> None:
    """A UI polling this must never catch a half-written document."""
    store.upsert_events([_ev("a")])
    write_snapshot(store)
    write_snapshot(store)
    leftovers = list(burn_home.glob("*.tmp"))
    assert not leftovers, f"temp file left behind: {leftovers}"
    assert json.loads(snapshot_path().read_text())


def test_empty_store_still_produces_a_valid_snapshot(store: Store) -> None:
    """A fresh install must render an empty state, not crash the menu bar."""
    payload = build_snapshot(store)
    assert payload["today"]["rows"] == []
    assert payload["today"]["subtotals"] == {}
    assert payload["current_window"] is None


def test_each_model_carries_its_project_breakdown(store: Store) -> None:
    """The app renders numbers but never derives them, so the drill-down has to
    arrive precomputed."""
    store.upsert_events(
        [
            _ev("a", cost=30.0),
            _ev("b", cost=10.0, minutes=1),
        ]
    )
    row = build_snapshot(store)["today"]["rows"][0]
    assert "projects" in row
    assert row["projects"][0]["project"] == "demo"
    assert row["projects"][0]["requests"] == 2


def test_project_shares_are_of_the_model_not_the_day(store: Store) -> None:
    """An expanded row answers "of this model's spend", which is the question
    expanding it asked."""
    events = [
        _ev("a", model="claude-opus-5", cost=30.0),
        _ev("b", model="claude-opus-5", cost=10.0, minutes=1),
        _ev("c", model="claude-fable-5", cost=960.0, minutes=2),
    ]
    for e, proj in zip(events, ("alpha", "beta", "alpha"), strict=True):
        e.project = proj
    store.upsert_events(events)

    opus = next(r for r in build_snapshot(store)["today"]["rows"] if r["model"] == "claude-opus-5")
    shares = {p["project"]: p["share_of_model"] for p in opus["projects"]}
    assert shares["alpha"] == pytest.approx(0.75), "30 of the model's own 40"
    assert shares["beta"] == pytest.approx(0.25)


def test_cache_payload_carries_the_counterfactual(store: Store) -> None:
    """The info panel explains the headline number with it, so it has to arrive
    precomputed — the app renders numbers, it never derives them."""
    store.upsert_events([_ev("a", tokens=TokenCounts(input=0, output=0, cache_read=1_000_000))])
    cache = build_snapshot(store)["ranges"][0]["cache"]
    assert cache["without_cache_usd"] is not None
    assert cache["actual_usd"] is not None
    assert cache["without_cache_usd"] > cache["actual_usd"]


def test_engine_pointer_prefers_a_path_that_survives_upgrades(tmp_path, monkeypatch):
    """Homebrew's Cellar path contains the version, so it breaks on every upgrade.

    The app cannot repair it either, because only the engine writes this file - so
    recording the versioned path deadlocks the popover on "could not refresh".
    """
    from burnometer.snapshot import _stable_path

    cellar = tmp_path / "Cellar" / "burn-o-meter" / "0.2.0" / "libexec" / "bin"
    cellar.mkdir(parents=True)
    real = cellar / "burnometer"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    stable_dir = tmp_path / "bin"
    stable_dir.mkdir()
    link = stable_dir / "burnometer"
    link.symlink_to(real)

    monkeypatch.setattr("burnometer.snapshot._STABLE_BIN_DIRS", (str(stable_dir),))
    assert _stable_path(str(real)) == str(link)


def test_engine_pointer_keeps_the_path_when_no_stable_one_exists(tmp_path, monkeypatch):
    """A venv install has no symlink to prefer; inventing one would be worse."""
    from burnometer.snapshot import _stable_path

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    exe = venv_bin / "burnometer"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    monkeypatch.setattr("burnometer.snapshot._STABLE_BIN_DIRS", (str(tmp_path / "nope"),))
    assert _stable_path(str(exe)) == str(exe)


def test_quota_from_an_expired_window_is_dropped():
    """A reading taken before its window reset describes a window that is gone.

    Showing "0% used" from a rolled-over window is not merely stale, it is wrong:
    the current window could be at any figure, and we have no observation of it.
    """
    from datetime import UTC, datetime, timedelta

    from burnometer.snapshot import _window_already_reset

    now = datetime.now(UTC)
    past = (now - timedelta(hours=2)).isoformat()
    future = (now + timedelta(days=20)).isoformat()
    observed = (now - timedelta(hours=38)).isoformat()

    # Reset already happened -> the reading cannot describe the current window.
    assert _window_already_reset(past, observed) is True
    # Reset is still ahead -> the reading still describes the live window, however
    # old it is; the UI shows its age rather than hiding it.
    assert _window_already_reset(future, observed) is False
    # Unparseable or missing timestamps must not silently drop a real figure.
    assert _window_already_reset(None, observed) is False
    assert _window_already_reset("not-a-date", observed) is False
