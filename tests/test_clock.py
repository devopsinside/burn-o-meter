"""Calendar boundaries in the user's time zone.

Every boundary was UTC until the menu bar showed nothing in India at 13:30:
usage at 01:21 local had been filed under yesterday, because "today" began at
UTC midnight - 05:30 there - and the hourly chart was labelled in UTC hours.
CI runs in UTC, where the two agree, which is how it went unnoticed; these
tests move the whole engine to other zones, and CI runs the suite in them too.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from burnometer.analytics import _bucket_labels, aggregate
from burnometer.clock import local_today, start_of_day, start_of_month
from burnometer.models import CostBasis, TokenCounts, UsageEvent
from burnometer.report import parse_since
from burnometer.snapshot import build_snapshot
from burnometer.store import Store


@pytest.fixture
def zone() -> Iterator:
    """Move this process - Python and SQLite alike - to another time zone."""
    before = os.environ.get("TZ")

    def to(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield to
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


#: The moment the bug was reported: 13:32 in India.
REPORTED = utc("2026-10-02T08:02:00")


@pytest.mark.parametrize(
    ("name", "midnight"),
    [
        ("UTC", "2026-10-02T00:00:00"),
        ("Asia/Kolkata", "2026-10-01T18:30:00"),  # +05:30
        ("Asia/Kathmandu", "2026-10-01T18:15:00"),  # +05:45
        ("America/Los_Angeles", "2026-10-02T07:00:00"),  # -07:00, still the 2nd there
        ("Pacific/Kiritimati", "2026-10-01T10:00:00"),  # +14:00, already the 2nd
    ],
)
def test_today_begins_at_the_users_midnight(zone, name: str, midnight: str) -> None:
    zone(name)
    assert start_of_day(REPORTED) == utc(midnight)
    assert parse_since("today") == start_of_day(datetime.now(UTC))


def test_midnight_on_a_clock_change_day_uses_that_days_offset(zone) -> None:
    """Zeroing now's clock would reuse now's offset, an hour out on these days."""
    zone("America/New_York")
    # Spring forward, 8 Mar 2026: midnight was EST (-5), the afternoon is EDT (-4).
    assert start_of_day(utc("2026-03-08T20:00:00")) == utc("2026-03-08T05:00:00")
    # Fall back, 1 Nov 2026: midnight was EDT (-4), the afternoon is EST (-5).
    assert start_of_day(utc("2026-11-01T20:00:00")) == utc("2026-11-01T04:00:00")


def test_month_and_bare_dates_are_local(zone) -> None:
    zone("Asia/Kolkata")
    assert start_of_month(REPORTED) == utc("2026-09-30T18:30:00")
    # A date the user types means their date.
    assert parse_since("2026-10-02") == utc("2026-10-01T18:30:00")
    assert local_today(utc("2026-10-01T19:51:24")).isoformat() == "2026-10-02"


def test_hour_labels_follow_the_clock_through_both_changes(zone) -> None:
    zone("America/New_York")
    spring = _bucket_labels("hour", utc("2026-03-08T05:00:00"), utc("2026-03-08T08:00:00"))
    assert spring == ["2026-03-08T00", "2026-03-08T01", "2026-03-08T03", "2026-03-08T04"]
    autumn = _bucket_labels("hour", utc("2026-11-01T04:00:00"), utc("2026-11-01T08:00:00"))
    assert autumn == ["2026-11-01T00", "2026-11-01T01", "2026-11-01T02", "2026-11-01T03"]


# -- the reported failure, end to end -----------------------------------------


def _event(key: str, ts: str, usd: float) -> UsageEvent:
    return UsageEvent(
        event_key=key,
        provider="claude_code",
        model="claude-opus-5",
        ts=utc(ts),
        tokens=TokenCounts(input=10, output=20),
        cost_usd=usd,
        cost_basis=CostBasis.API_EQUIVALENT,
        price_source="test",
    )


@pytest.fixture
def night_owl(tmp_path: Path) -> Store:
    """Usage late on the 1st and in the small hours of the 2nd, India time."""
    store = Store.open(tmp_path / "burn.db")
    store.upsert_events(
        [
            _event("evening", "2026-10-01T17:00:00", 1.0),  # 22:30 on the 1st, IST
            _event("small-hours", "2026-10-01T19:51:24", 2.0),  # 01:21 on the 2nd, IST
        ]
    )
    yield store
    store.close()


def test_the_menu_bar_counts_small_hours_usage_as_today(zone, night_owl: Store) -> None:
    zone("Asia/Kolkata")
    snap = build_snapshot(night_owl, now=REPORTED)

    assert snap["today"]["subtotals"]["api_equivalent"]["requests"] == 1
    assert snap["today"]["subtotals"]["api_equivalent"]["cost_usd"] == 2.0
    today = next(r for r in snap["ranges"] if r["key"] == "today")
    assert today["points"][0]["label"] == "2026-10-02T00", "the chart starts at local midnight"
    spent = [p["label"] for p in today["points"] if p["total"]]
    assert spent == ["2026-10-02T01"], "the bar sits at 01:00 local, not 19:00 UTC"


def test_days_are_the_users_days(zone, night_owl: Store) -> None:
    zone("Asia/Kolkata")
    by_day = {row.key: row.totals.requests for row in aggregate(night_owl, "day").rows}
    assert by_day == {"2026-10-01": 1, "2026-10-02": 1}

    zone("UTC")
    by_day = {row.key: row.totals.requests for row in aggregate(night_owl, "day").rows}
    assert by_day == {"2026-10-01": 2}


def test_in_utc_the_same_usage_is_still_yesterday(zone, night_owl: Store) -> None:
    """The control: the fix moves the boundary to the user, it does not widen it."""
    zone("UTC")
    assert build_snapshot(night_owl, now=REPORTED)["today"]["subtotals"] == {}


def test_the_cli_today_agrees_with_the_menu_bar(
    zone, night_owl: Store, burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from burnometer.cli import main

    zone("Asia/Kolkata")
    night_owl.close()
    burn_home.mkdir(parents=True, exist_ok=True)
    (burn_home / "burn.db").write_bytes(Path(night_owl.path).read_bytes())

    now = datetime.now(UTC)
    with Store.open(burn_home / "burn.db") as store:
        # Re-date the pair to tonight, so "today" is real wall-clock today.
        midnight = start_of_day(now)
        store._conn.execute(
            "UPDATE usage_events SET ts = ? WHERE event_key = 'small-hours'",
            ((midnight + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),),
        )
        store._conn.execute(
            "UPDATE usage_events SET ts = ? WHERE event_key = 'evening'",
            ((midnight - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),),
        )
        store._conn.commit()

    assert main(["today", "--json"]) == 0
    today = json.loads(capsys.readouterr().out)["today"]
    # One minute after local midnight is today; one minute before is not.
    assert today["subtotals"]["api_equivalent"]["requests"] == 1
    assert today["since"] == midnight.isoformat()
