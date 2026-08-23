"""Claude plan utilisation: the only exact Claude quota available locally.

Claude Code stores no quota anywhere and Anthropic publishes no token limit for
subscription plans, so a percentage cannot be derived from usage. The desktop
app records the utilisation the service reports back, which is a percentage
already — no denominator to invent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from burnometer.adapters.claude_desktop import HISTORY_DAYS, ClaudeDesktopAdapter
from burnometer.models import QuotaSource

FIXTURES = Path(__file__).parent / "fixtures" / "claude_desktop"


@pytest.fixture
def adapter() -> ClaudeDesktopAdapter:
    return ClaudeDesktopAdapter()


@pytest.fixture
def parsed(adapter: ClaudeDesktopAdapter):
    return adapter.parse(FIXTURES / "plan-usage-history.json", FIXTURES)


def _window(parsed, name: str):
    return sorted((q for q in parsed.quotas if q.window_name == name), key=lambda q: q.observed_at)


# -- what it extracts ------------------------------------------------------


def test_reads_both_windows(parsed) -> None:
    assert {q.window_name for q in parsed.quotas} == {"five_hour", "seven_day"}


def test_readings_are_exact_not_derived(parsed) -> None:
    """These are Anthropic's own percentages, so nothing here is an estimate."""
    assert all(q.source is QuotaSource.EXACT for q in parsed.quotas)
    assert all(q.is_exact for q in parsed.quotas)


def test_latest_five_hour_reading(parsed) -> None:
    latest = _window(parsed, "five_hour")[-1]
    assert latest.used_percent == 16.0
    assert latest.window_minutes == 300


def test_weekly_window_is_seven_days(parsed) -> None:
    assert _window(parsed, "seven_day")[-1].window_minutes == 7 * 24 * 60


def test_window_reset_is_preserved(parsed) -> None:
    """The five-hour figure sawtooths. Smoothing that away would hide the reset
    the user most needs to see."""
    values = [q.used_percent for q in _window(parsed, "five_hour")]
    assert 98.0 in values and 7.0 in values
    assert values.index(98.0) < values.index(7.0)


def test_every_reading_carries_its_own_timestamp(parsed) -> None:
    """Samples land roughly every 15 minutes, so a reading can sit behind what
    the Claude app shows live. Without the timestamp the UI cannot say so."""
    now = datetime.now(UTC)
    for quota in parsed.quotas:
        assert quota.observed_at <= now
        assert quota.observed_at.tzinfo is not None


# -- what it refuses to extract -------------------------------------------


def test_org_identifier_is_never_read(parsed) -> None:
    """`org` identifies the account and adds nothing to a usage meter."""
    for quota in parsed.quotas:
        for value in vars(quota).values() if hasattr(quota, "__dict__") else []:
            assert "CANARY" not in str(value)
    blob = " ".join(
        str(getattr(quota, field))
        for quota in parsed.quotas
        for field in ("provider", "window_name", "plan_type")
    )
    assert "CANARY" not in blob


def test_plan_type_is_not_guessed(parsed) -> None:
    """The file does not record the tier, and inferring it from the numbers
    would be invention."""
    assert all(q.plan_type is None for q in parsed.quotas)


def test_no_usage_events_are_produced(parsed) -> None:
    """This source carries utilisation only; tokens come from the transcripts."""
    assert parsed.events == []


# -- resilience ------------------------------------------------------------


def test_out_of_range_and_malformed_samples_are_skipped(parsed) -> None:
    percentages = [q.used_percent for q in parsed.quotas]
    assert all(0 <= p <= 100 for p in percentages), "150% is not a reading"
    assert parsed.lines_skipped >= 3


def test_history_is_bounded(parsed) -> None:
    """A 10-day-old sample sits outside the retention window."""
    cutoff = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)
    assert all(q.observed_at >= cutoff for q in parsed.quotas)
    assert 50.0 not in [q.used_percent for q in parsed.quotas]


def test_missing_file_is_not_an_error(adapter: ClaudeDesktopAdapter, tmp_path: Path) -> None:
    """Not everyone runs the Claude desktop app."""
    source = adapter.sources()[0]
    assert list(source.discover()) is not None
    empty = tmp_path / "nothing"
    empty.mkdir()
    from burnometer.adapters.base import LogSource

    assert list(LogSource(root=empty, glob="plan-usage-history.json").discover()) == []


def test_unexpected_document_shape_is_survivable(
    adapter: ClaudeDesktopAdapter, tmp_path: Path
) -> None:
    path = tmp_path / "plan-usage-history.json"
    path.write_text(json.dumps({"version": 3, "samples": "not-a-list"}))
    result = adapter.parse(path, tmp_path)
    assert result.quotas == []
    assert result.lines_skipped == 1


# -- when the window rolls -------------------------------------------------
#
# Anthropic reports a percentage and nothing else — no reset time, unlike Codex.
# The series carries it, but only for the five-hour window.


def _snap(minutes_ago: int, percent: float, window: str = "five_hour"):
    from burnometer.models import QuotaSnapshot, QuotaSource

    return QuotaSnapshot(
        provider="claude",
        window_name=window,
        used_percent=percent,
        observed_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source=QuotaSource.EXACT,
        window_minutes=300,
    )


def test_window_start_found_after_an_idle_gap() -> None:
    """The common case, and the one a naive look-for-a-drop rule misses: the
    counter sits at zero, then rises on first use."""
    from burnometer.adapters.claude_desktop import _attach_reset_times

    series = [_snap(200, 0), _snap(185, 0), _snap(170, 19), _snap(155, 40), _snap(140, 60)]
    _attach_reset_times(series)
    assert series[-1].resets_at is not None
    # Started between the 185- and 170-minute samples, so ~2h55m of the 5h left.
    remaining = (series[-1].resets_at - datetime.now(UTC)).total_seconds() / 60
    assert 110 < remaining < 135, remaining


def test_window_start_found_at_a_roll_under_continuous_use() -> None:
    """98% → 7% is a window rolling straight into the next one."""
    from burnometer.adapters.claude_desktop import _attach_reset_times

    series = [_snap(120, 90), _snap(105, 98), _snap(90, 7), _snap(75, 20)]
    _attach_reset_times(series)
    remaining = (series[-1].resets_at - datetime.now(UTC)).total_seconds() / 60
    assert 200 < remaining < 220, remaining


def test_a_naive_drop_rule_would_have_been_stale() -> None:
    """Guard against regressing to it: here the last drop is long past, and only
    the later idle-then-rise marks the window actually in force."""
    from burnometer.adapters.claude_desktop import _attach_reset_times

    series = [_snap(700, 29), _snap(690, 0), _snap(400, 0), _snap(170, 19), _snap(140, 60)]
    _attach_reset_times(series)
    remaining = (series[-1].resets_at - datetime.now(UTC)).total_seconds() / 60
    assert remaining > 0, "a drop-based rule would put the reset hours in the past"


def test_no_reset_is_claimed_for_the_weekly_window() -> None:
    """Six days of real data showed one drop, straight to zero — a scheduled
    reset, not one triggered by use. One observation cannot establish a period,
    and a wrong 'resets in' is worse than none."""
    from burnometer.adapters.claude_desktop import _attach_reset_times

    series = [_snap(m, p, window="seven_day") for m, p in ((200, 0), (170, 10), (140, 25))]
    _attach_reset_times(series)
    assert all(q.resets_at is None for q in series)


def test_only_the_newest_reading_carries_a_reset(parsed) -> None:
    """Older readings describe windows that have already gone."""
    five = sorted(
        (q for q in parsed.quotas if q.window_name == "five_hour"),
        key=lambda q: q.observed_at,
    )
    assert all(q.resets_at is None for q in five[:-1])
