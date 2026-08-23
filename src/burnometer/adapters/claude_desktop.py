"""Claude plan utilisation — ``~/Library/Application Support/Claude/plan-usage-history.json``.

This is the only source of **exact** Claude quota available without credentials
or a network call, and it changes what this tool can honestly say.

Claude Code itself stores no quota anywhere (verified across every transcript,
config file and CLI subcommand). Anthropic publishes no token limit for
subscription plans, so a percentage cannot be derived from usage — there is no
denominator. But the Claude desktop app records the utilisation figures the
service reports back, and those are percentages already:

    {"t": 1787391480000, "org": "…", "u": {"fh": 98, "sd": 20}}

``fh`` is the rolling five-hour window, ``sd`` the seven-day one, both 0–100.
Verified against 3,163 real samples: values stay inside 0–100, samples land
roughly every 15 minutes, and ``fh`` sawtooths — 21 resets in 30 days, e.g.
98% → 7% — exactly as a five-hour window should.

Two things this source is not:

* **It is not Claude Code specific.** The limit is shared between Claude Code
  and Claude chat, so the figure covers the whole account. That is the number a
  user actually needs, but it is not attributable to one tool.
* **It is not live unless the desktop app is.** Samples are only written while
  Claude is running. Every reading therefore carries its own timestamp and the
  UI reports its age rather than presenting an old percentage as current.

``org`` is deliberately never read: it identifies the account, adds nothing to a
usage meter, and would be one more identifier sitting in our database.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..models import QuotaSnapshot, QuotaSource
from ..safety import (
    AdapterError,
    open_log_readonly,
    pluck_float,
    pluck_int,
    pluck_mapping,
    redact_path,
)
from .base import LogSource, ParseResult, register

PROVIDER = "claude"

#: Window name -> (json key, minutes). Anthropic's own labels for these are the
#: five-hour session and the weekly cap.
WINDOWS = {
    "five_hour": ("fh", 5 * 60),
    "seven_day": ("sd", 7 * 24 * 60),
}

#: Windows whose reset time can be derived from the series.
#:
#: Only the five-hour one. It sawtooths clearly and opens on first use, so the
#: start of the current rising run is its start. The weekly cap behaves
#: differently — across six days of real data it dropped exactly once, straight
#: to zero, which looks like a scheduled reset rather than one triggered by use.
#: One observation is not enough to establish a period, and applying the
#: five-hour rule to it produced an answer five hours adrift. So no reset time is
#: claimed for it; a wrong "resets in" is worse than none, because the whole
#: point of the number is deciding whether to wait.
DERIVABLE_RESET_WINDOWS = frozenset({"five_hour"})

#: Keep a bounded slice of history rather than all 30 days on every scan. Enough
#: to chart a trend, cheap enough to re-read whole.
HISTORY_DAYS = 7


class ClaudeDesktopAdapter:
    name = PROVIDER
    display_name = "Claude (plan usage)"
    implemented = True
    # The reset time is computed from the whole sample series, so this file can
    # yield a new answer without changing. It is one small JSON document.
    rescan_unchanged = True

    def sources(self) -> Sequence[LogSource]:
        root = Path.home() / "Library" / "Application Support" / "Claude"
        # One named file, not a glob over a directory that also holds config and
        # extension data.
        return [LogSource(root=root, glob="plan-usage-history.json")]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        quotas: list[QuotaSnapshot] = []
        skipped = 0

        try:
            with open_log_readonly(root, path) as fh:
                document = json.loads(fh.read())
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(
                redact_path(path), 0, f"{type(exc).__name__} while reading plan usage"
            ) from None

        samples = document.get("samples") if isinstance(document, dict) else None
        if not isinstance(samples, list):
            return ParseResult(events=[], quotas=[], offset=0, lines_skipped=1)

        cutoff = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)

        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                skipped += 1
                continue

            millis = pluck_int(sample, "t")
            if not millis:
                skipped += 1
                continue
            try:
                observed = datetime.fromtimestamp(millis / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                skipped += 1
                continue
            if observed < cutoff:
                continue

            usage = pluck_mapping(sample, "u")
            if usage is None:
                skipped += 1
                continue

            for window_name, (key, minutes) in WINDOWS.items():
                if key not in usage:
                    continue
                # pluck_float returns None for anything non-numeric, which
                # pluck_int cannot express: it would hand back 0 and we would
                # record "0% used" — claiming the window is empty when what we
                # actually have is a value we could not read.
                value = pluck_float(usage, key)
                if value is None:
                    skipped += 1
                    continue
                percent = int(value)
                if percent < 0 or percent > 100:
                    # Outside the documented range; treating it as a reading
                    # would be asserting something we cannot support.
                    skipped += 1
                    continue
                quotas.append(
                    QuotaSnapshot(
                        provider=PROVIDER,
                        window_name=window_name,
                        used_percent=float(percent),
                        observed_at=observed,
                        source=QuotaSource.EXACT,
                        window_minutes=minutes,
                        # The plan tier is not recorded in this file, and
                        # guessing it from the numbers would be invention.
                        plan_type=None,
                    )
                )
            del index

        _attach_reset_times(quotas)

        return ParseResult(
            events=[],  # utilisation only; token usage comes from the transcripts
            quotas=quotas,
            offset=0,  # small file, always read whole
            lines_read=len(samples),
            lines_skipped=skipped,
        )


def _attach_reset_times(quotas: list[QuotaSnapshot]) -> None:
    """Work out when each window will roll, and stamp it on the newest reading.

    Anthropic reports a utilisation percentage and nothing else — no reset time,
    unlike Codex. But the series carries the answer: a window opens on first use
    after the previous one expired, so the current window began where the latest
    run of non-decreasing readings began.

    Two boundaries end a run. The value *drops* (98% → 7%) when a window rolls
    straight into a new one under continuous use. Or it sits at *zero* and then
    rises, which is a window opening after an idle gap — the more common case,
    and the one a naive "look for a drop" rule misses entirely, leaving a reset
    time hours in the past.

    Precision: samples land roughly 15 minutes apart, so first use is known only
    to lie between the last zero and the first non-zero reading. The midpoint is
    used, which bounds the error at about seven minutes either way. Every
    surface renders it with a ``~``.
    """
    by_window: dict[str, list[QuotaSnapshot]] = {}
    for q in quotas:
        by_window.setdefault(q.window_name, []).append(q)

    for window_name, series in by_window.items():
        if window_name not in DERIVABLE_RESET_WINDOWS:
            continue
        series.sort(key=lambda q: q.observed_at)
        start = _window_start(series)
        if start is None:
            continue
        minutes = WINDOWS[window_name][1]
        # Only the newest reading describes the window in force.
        series[-1].resets_at = start + timedelta(minutes=minutes)


def _window_start(series: list[QuotaSnapshot]) -> datetime | None:
    """When the current window opened, to within the sampling interval."""
    if len(series) < 2:
        return None

    index = len(series) - 1
    while index > 0:
        previous = series[index - 1].used_percent or 0.0
        current = series[index].used_percent or 0.0
        if current < previous:
            # A roll straight into a new window under continuous use.
            return series[index].observed_at
        if previous == 0.0:
            # Idle, then first use. The true start lies between the two samples.
            gap = series[index].observed_at - series[index - 1].observed_at
            return series[index - 1].observed_at + gap / 2
        index -= 1

    return series[0].observed_at


register(ClaudeDesktopAdapter())
