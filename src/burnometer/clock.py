"""Calendar boundaries in the user's own time zone.

Timestamps are stored in UTC, which is right for storage and wrong for a
calendar. "Today" is since the user's midnight, and an hourly chart is labelled
in the user's hours. Everything here used UTC until the menu bar showed nothing
at half past one in the afternoon in India: usage at 01:21 local time had been
filed under yesterday, because "today" began at UTC midnight - 05:30 there - and
every bar of the hourly chart sat five and a half hours off.

The one rule: **no caller computes a day boundary itself.** Python's local zone
and SQLite's ``'localtime'`` modifier both read the process's ``TZ``, so the two
halves always agree, and a test can move the whole engine to another zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

__all__ = [
    "LOCAL_DAY_SQL",
    "LOCAL_HOUR_SQL",
    "local_label",
    "local_midnight",
    "local_today",
    "start_of_day",
    "start_of_month",
]

#: Bucket expressions over the stored UTC ``ts``. SQLite reads the trailing ``Z``
#: and converts with the same zone rules Python uses.
LOCAL_DAY_SQL = "strftime('%Y-%m-%d', ts, 'localtime')"
LOCAL_HOUR_SQL = "strftime('%Y-%m-%dT%H', ts, 'localtime')"

_LABEL_FORMATS = {"day": "%Y-%m-%d", "hour": "%Y-%m-%dT%H"}


def local_today(now: datetime) -> date:
    """The user's calendar date at ``now``."""
    return now.astimezone().date()


def local_midnight(day: date) -> datetime:
    """The UTC instant at which ``day`` begins where the user is.

    Built from the date rather than by zeroing ``now``'s clock: on the day a
    clock change happens, midnight and now have different UTC offsets, and
    reusing now's offset would put midnight an hour out.
    """
    return datetime(day.year, day.month, day.day).astimezone().astimezone(UTC)


def start_of_day(now: datetime, days_back: int = 0) -> datetime:
    """Local midnight today, or ``days_back`` local days earlier."""
    return local_midnight(local_today(now) - timedelta(days=days_back))


def start_of_month(now: datetime) -> datetime:
    """Local midnight on the first of the user's current month."""
    return local_midnight(local_today(now).replace(day=1))


def local_label(instant: datetime, bucket: str) -> str:
    """The bucket label ``instant`` falls in, matching the SQL expressions."""
    return instant.astimezone().strftime(_LABEL_FORMATS[bucket])
