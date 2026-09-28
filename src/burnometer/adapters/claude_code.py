"""Claude Code adapter — ``~/.claude/projects/<project-slug>/<sessionId>.jsonl``,
plus each session's subagent transcripts in ``<sessionId>/subagents/``.

Three properties of this format drive the implementation, all verified against
real transcripts rather than assumed:

**Records repeat.** Claude Code writes the same assistant message into the
transcript several times — we measured 2,405 records collapsing to 958 unique,
with one message appearing seven times. In a session's transcript the usage
payloads are byte-identical across repeats, so ``(requestId, message.id)``
deduplicates safely. Summing without it overcounts by roughly 2.5x.

In a *subagent's* transcript they are not: a message is written while it
streams, and early copies carry only part of the output (all 13 repeated
subagent messages measured, 49 output tokens in the first copies against 17,686
in the last; input and cache fields identical). Output only rises, so the copy
with the most output is kept — here, and in the store across scans.

**Cache writes carry a TTL split.** ``usage.cache_creation`` separates
``ephemeral_5m_input_tokens`` from ``ephemeral_1h_input_tokens``, and the two
bill at different multipliers (1.25x and 2.0x base input). Older versions emit
only the flat ``cache_creation_input_tokens``; that is treated as 5-minute,
which is the conservative reading — it under-attributes rather than inventing a
premium that may not have been charged.

**``usage.iterations`` mirrors its parent.** It restates the same counts and
must be ignored, or every record with one doubles.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ..models import TokenCounts, UsageEvent
from ..safety import (
    AdapterError,
    open_log_readonly,
    pluck_int,
    pluck_mapping,
    pluck_str,
    project_label,
    redact_path,
)
from .base import LogSource, ParseResult, register, resolve_roots

PROVIDER = "claude_code"

_SYNTHETIC = "<synthetic>"
_PREFILTER = b'"assistant"'


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` rather than raising."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ClaudeCodeAdapter:
    name = PROVIDER
    display_name = "Claude Code"
    implemented = True

    # Offsets make a rescan of an unchanged file pointless here: every
    # answer is read off individual lines, not derived from the series.
    rescan_unchanged = False
    #: Claude Code writes to one of these depending on how it was installed, and
    #: honours CLAUDE_CONFIG_DIR to move them. Checking only the first would
    #: report "not installed" for anyone on the second.
    ENV_VAR = "CLAUDE_CONFIG_DIR"
    DEFAULT_ROOTS = (
        Path.home() / ".claude",
        Path.home() / ".config" / "claude",
    )

    def sources(self) -> Sequence[LogSource]:
        # Exact shapes, never a recursive walk — ~/.claude/sessions holds *.key
        # files, and a session's own directory holds tool output and notes.
        #
        # Subagents write their turns to <session>/subagents/agent-<id>.jsonl, not
        # to the session's transcript: measured on real sessions, none of their
        # messages appear in the parent file. Reading only <session>.jsonl left
        # every subagent turn out of the totals. The pattern names the transcript
        # exactly, so the agent-<id>.meta.json beside it is not matched either.
        return [
            LogSource(root=root / "projects", glob=glob, env_var=self.ENV_VAR)
            for root in resolve_roots(self.ENV_VAR, self.DEFAULT_ROOTS)
            for glob in ("*/*.jsonl", "*/*/subagents/agent-*.jsonl")
        ]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        events: list[UsageEvent] = []
        seen: dict[str, int] = {}  # event key -> index into events
        lines_read = 0
        lines_skipped = 0
        duplicates = 0
        pos = offset

        with open_log_readonly(root, path) as fh:
            if offset:
                fh.seek(offset)

            lineno = 0
            for raw in fh:
                lineno += 1

                # A line without a terminator is still being written. Stop here
                # and do NOT advance past it, so the next scan re-reads it whole.
                # Without this, a scan racing an active session would silently
                # drop the record it caught mid-write.
                if not raw.endswith(b"\n"):
                    break
                pos += len(raw)
                lines_read += 1

                if _PREFILTER not in raw:
                    continue

                try:
                    event = self._record_to_event(raw, path, lineno, project_mode)
                except AdapterError:
                    raise
                except Exception as exc:  # noqa: BLE001 — see below
                    # Re-raised with the original detached so no fragment of a
                    # transcript can reach a traceback (G6).
                    raise AdapterError(
                        redact_path(path), lineno, f"{type(exc).__name__} while parsing"
                    ) from None

                if event is None:
                    lines_skipped += 1
                    continue
                if event.event_key in seen:
                    duplicates += 1
                    # Subagent transcripts write a message while it streams, so
                    # an early copy can carry a fraction of the final output
                    # (measured: 49 tokens where the last copy said 17,686).
                    # Output only ever rises through the copies, so the largest
                    # is the finished figure; for the session transcript, whose
                    # copies are identical, this keeps the first as before.
                    index = seen[event.event_key]
                    if event.tokens.output > events[index].tokens.output:
                        events[index] = event
                    continue
                seen[event.event_key] = len(events)
                events.append(event)

        return ParseResult(
            events=events,
            quotas=[],  # Claude Code does not persist rate-limit data anywhere.
            offset=pos,
            lines_read=lines_read,
            lines_skipped=lines_skipped,
            duplicates_dropped=duplicates,
        )

    # -- record -> event ---------------------------------------------------

    @staticmethod
    def _record_to_event(
        raw: bytes, path: Path, lineno: int, project_mode: str
    ) -> UsageEvent | None:
        """Return an event, or ``None`` if this record is not billable usage.

        Every value is lifted with a ``pluck_*`` helper, which returns scalars
        only. The parsed record is never copied and no content field is ever
        named here, so prompt text has no route out of this function (G1).
        """
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        if record.get("type") != "assistant":
            return None

        message = pluck_mapping(record, "message")
        usage = pluck_mapping(message, "usage")
        if usage is None:
            return None

        model = pluck_str(message, "model", max_len=128)
        if not model or model == _SYNTHETIC:
            # Synthetic messages are Claude Code's own error text. They are not
            # billed and have no model to price.
            return None

        ts = _parse_ts(pluck_str(record, "timestamp", max_len=64))
        if ts is None:
            return None

        # Dedup key, most specific first. message.id is already unique per API
        # response, so it alone is sufficient when requestId is absent — which
        # happens in a small number of real records.
        message_id = pluck_str(message, "id", max_len=128)
        request_id = pluck_str(record, "requestId", max_len=128)
        fallback = pluck_str(record, "uuid", max_len=128)
        if message_id and request_id:
            key = f"{PROVIDER}:{request_id}:{message_id}"
        elif message_id:
            key = f"{PROVIDER}:{message_id}"
        elif fallback:
            key = f"{PROVIDER}:{fallback}"
        else:
            return None

        cache_creation = pluck_mapping(usage, "cache_creation")
        if cache_creation is not None:
            cw_5m = pluck_int(cache_creation, "ephemeral_5m_input_tokens")
            cw_1h = pluck_int(cache_creation, "ephemeral_1h_input_tokens")
        else:
            # Pre-split format. Attribute to the 5-minute rate: it is the lower
            # multiplier, so an unknown TTL never inflates the bill.
            cw_5m = pluck_int(usage, "cache_creation_input_tokens")
            cw_1h = 0

        tokens = TokenCounts(
            # Claude Code already reports input excluding cache reads, so no
            # subtraction is needed here (unlike Codex).
            input=pluck_int(usage, "input_tokens"),
            output=pluck_int(usage, "output_tokens"),
            reasoning=0,  # not reported separately by this provider
            cache_read=pluck_int(usage, "cache_read_input_tokens"),
            cache_write_5m=cw_5m,
            cache_write_1h=cw_1h,
        )

        return UsageEvent(
            event_key=key,
            provider=PROVIDER,
            model=model,
            ts=ts,
            tokens=tokens,
            session_id=pluck_str(record, "sessionId", max_len=128),
            project=project_label(pluck_str(record, "cwd", max_len=4096), project_mode),
            git_branch=pluck_str(record, "gitBranch", max_len=256),
            raw_file=redact_path(path),
            raw_line=lineno,
        )


register(ClaudeCodeAdapter())
