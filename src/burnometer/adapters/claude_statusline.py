"""Claude plan utilisation as Claude Code last reported it.

Read from the file ``burnometer statusline`` writes (see
:mod:`burnometer.statusline`), which holds the figures Claude Code received from
the service on its most recent turn. Same account-wide limit as the desktop
app's plan records, so both report under provider ``claude`` and the newest
reading of each window wins - here, typically, because this one is written on
every turn rather than every quarter-hour.

The reset time is the service's own, not derived from a series, so it is
attached to every reading rather than inferred for the newest.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from ..config import burn_home
from ..models import QuotaSnapshot, QuotaSource
from ..safety import AdapterError, open_log_readonly, pluck_float, pluck_mapping, redact_path
from ..statusline import CAPTURE_NAME, WINDOWS
from .base import LogSource, ParseResult, register

PROVIDER = "claude"

#: Window name -> minutes, as the desktop adapter records them.
WINDOW_MINUTES = {"five_hour": 5 * 60, "seven_day": 7 * 24 * 60}

#: Well past any real capture, which is a few hundred bytes.
MAX_BYTES = 64 * 1024


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class ClaudeStatuslineAdapter:
    name = "claude_statusline"
    display_name = "Claude (live, via Claude Code)"
    implemented = True
    rescan_unchanged = False
    #: Shown by ``doctor`` when the file does not exist yet: it is written only
    #: once the status line hook is installed and Claude Code has taken a turn.
    missing_hint = "run `burn-o-meter statusline install`, then use Claude Code"

    def sources(self) -> Sequence[LogSource]:
        return [LogSource(root=burn_home(), glob=CAPTURE_NAME)]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        try:
            with open_log_readonly(root, path) as fh:
                document = json.loads(fh.read(MAX_BYTES))
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(
                redact_path(path), 0, f"{type(exc).__name__} while reading rate limits"
            ) from None

        windows = pluck_mapping(document, "windows") if isinstance(document, Mapping) else None
        quotas: list[QuotaSnapshot] = []
        skipped = 0
        for window in WINDOWS:
            entry = pluck_mapping(windows, window)
            if entry is None:
                continue
            percent = pluck_float(entry, "used_percent")
            observed = _when(entry.get("observed_at"))
            if percent is None or not 0 <= percent <= 100 or observed is None:
                skipped += 1
                continue
            quotas.append(
                QuotaSnapshot(
                    provider=PROVIDER,
                    window_name=window,
                    used_percent=percent,
                    observed_at=observed,
                    source=QuotaSource.EXACT,
                    window_minutes=WINDOW_MINUTES[window],
                    resets_at=_when(entry.get("resets_at")),
                    plan_type=None,
                )
            )

        return ParseResult(
            events=[],
            quotas=quotas,
            offset=0,  # one small document, always read whole
            lines_read=1,
            lines_skipped=skipped,
        )


register(ClaudeStatuslineAdapter())
