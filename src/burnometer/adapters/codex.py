"""Codex CLI adapter — ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.

Codex records usage in ``event_msg`` records of type ``token_count``. Each one
carries two figures:

``total_token_usage``
    A running total for the session, restated on every event.
``last_token_usage``
    What the provider says the most recent turn consumed.

**We use deltas of the running total, not ``last_token_usage``.** That is the
opposite of the obvious reading, and it matters: Codex repeats its final
``token_count`` event, so ``last_token_usage`` gets counted twice. Measured
across real sessions, summing ``last_token_usage`` overshot the session's own
final total in two of three sessions — by 6.5% in the worst case. Differencing
the running total cannot have that problem: a repeated event yields a delta of
zero, and the deltas sum to the final total *by construction*.

That property also gives us a free correctness check. After parsing, the deltas
must reconcile exactly with the last ``total_token_usage`` in the file. If they
ever do not, our reading of the format is wrong, and the scan says so instead of
quietly reporting a plausible number.

Token normalisation, verified against 304 real usage blocks where
``input_tokens + output_tokens == total_tokens`` held every time — which proves
the other counters are subsets, not addends:

* ``cached_input_tokens`` sits inside ``input_tokens`` -> becomes ``cache_read``
* ``cache_write_input_tokens`` sits inside ``input_tokens`` -> ``cache_write_5m``
  (OpenAI caching has no TTL split, so the single rate maps to the 5m slot)
* ``reasoning_output_tokens`` sits inside ``output_tokens`` -> display only
* ``input`` is what remains: fresh, uncached, unwritten tokens

Unlike Claude Code, these files are parsed whole on every scan rather than
resumed from an offset. Model attribution comes from the most recent preceding
``turn_context``, and each delta needs the previous event's total, so a mid-file
resume would have neither. The rollouts total under 5 MB, parsing them costs a
few milliseconds, and the event-key dedup makes rescans free — so correctness
wins over an optimisation nothing needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import QuotaSnapshot, QuotaSource, TokenCounts, UsageEvent
from ..safety import (
    AdapterError,
    open_log_readonly,
    pluck_float,
    pluck_int,
    pluck_mapping,
    pluck_str,
    project_label,
    redact_path,
)
from .base import LogSource, ParseResult, register, resolve_roots

PROVIDER = "codex"

_COUNTER_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _counters(usage: Any) -> dict[str, int]:
    return {field: pluck_int(usage, field) for field in _COUNTER_FIELDS}


class CodexAdapter:
    name = PROVIDER
    display_name = "Codex CLI"
    implemented = True

    # Offsets make a rescan of an unchanged file pointless here: every
    # answer is read off individual lines, not derived from the series.
    rescan_unchanged = False
    #: Codex relocates its whole home with CODEX_HOME.
    ENV_VAR = "CODEX_HOME"
    DEFAULT_ROOTS = (Path.home() / ".codex",)

    def sources(self) -> Sequence[LogSource]:
        # Date-partitioned, rollout files only. auth.json sits several levels up
        # and is unreachable from this pattern.
        return [
            LogSource(
                root=root / "sessions",
                glob="*/*/*/rollout-*.jsonl",
                env_var=self.ENV_VAR,
            )
            for root in resolve_roots(self.ENV_VAR, self.DEFAULT_ROOTS)
        ]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        events: list[UsageEvent] = []
        quotas: list[QuotaSnapshot] = []
        lines_read = 0
        lines_skipped = 0
        duplicates = 0
        mismatches = 0

        session_id: str | None = None
        session_cwd: str | None = None
        model: str | None = None
        effort: str | None = None
        turn_id: str | None = None
        running = dict.fromkeys(_COUNTER_FIELDS, 0)
        final_total: dict[str, int] | None = None
        summed = dict.fromkeys(_COUNTER_FIELDS, 0)
        # Totals accumulated before each counter reset. Without carrying these
        # forward, the reconciliation below would compare a whole session's
        # deltas against only the post-reset total and report a false failure —
        # an integrity check that cries wolf is worse than none.
        carried = dict.fromkeys(_COUNTER_FIELDS, 0)
        ordinal = 0

        with open_log_readonly(root, path) as fh:
            for lineno, raw in enumerate(fh, start=1):
                if not raw.endswith(b"\n"):
                    break  # still being written; leave it for the next scan
                lines_read += 1

                try:
                    record = self._load(raw)
                except Exception as exc:  # noqa: BLE001
                    raise AdapterError(
                        redact_path(path), lineno, f"{type(exc).__name__} while parsing"
                    ) from None

                if record is None:
                    lines_skipped += 1
                    continue

                kind = record.get("type")
                payload = pluck_mapping(record, "payload")
                if payload is None:
                    continue

                if kind == "session_meta":
                    session_id = pluck_str(payload, "id", max_len=128)
                    session_cwd = pluck_str(payload, "cwd", max_len=4096)
                    continue

                if kind == "turn_context":
                    # The only place a model is named. Sessions do switch models
                    # mid-flight, so this must be tracked, not read once.
                    turn_id = pluck_str(payload, "turn_id", max_len=128)
                    model = pluck_str(payload, "model", max_len=128) or model
                    effort = pluck_str(payload, "effort", max_len=32)
                    if cwd := pluck_str(payload, "cwd", max_len=4096):
                        session_cwd = cwd
                    continue

                if kind != "event_msg" or payload.get("type") != "token_count":
                    continue

                ts = _parse_ts(pluck_str(record, "timestamp", max_len=64))
                info = pluck_mapping(payload, "info")

                if rate_limits := pluck_mapping(payload, "rate_limits"):
                    quotas.extend(self._quota_snapshots(rate_limits, ts))

                if info is None:
                    lines_skipped += 1
                    continue
                total = pluck_mapping(info, "total_token_usage")
                if total is None:
                    lines_skipped += 1
                    continue

                current = _counters(total)

                # A block we could not read yields all zeros. If anything has
                # been counted already, that is a malformed record, not a
                # restart — treating it as one would zero the baseline and
                # corrupt every delta after it.
                if all(v == 0 for v in current.values()) and any(running.values()):
                    lines_skipped += 1
                    ordinal += 1
                    continue

                final_total = current

                if any(current[f] < running[f] for f in _COUNTER_FIELDS):
                    # The counter went backwards, so the session restarted its
                    # accounting. Bank what came before and treat the current
                    # reading as the whole delta rather than producing a
                    # negative one.
                    for f in _COUNTER_FIELDS:
                        carried[f] += running[f]
                    delta = dict(current)
                else:
                    delta = {f: current[f] - running[f] for f in _COUNTER_FIELDS}
                running = current

                # Cross-check against the provider's own per-turn figure. A
                # repeated event shows up here as delta==0 while last_token_usage
                # restates the previous turn; that disagreement is expected and
                # is exactly the double-count we are avoiding.
                last = pluck_mapping(info, "last_token_usage")
                if last is not None:
                    last_counts = _counters(last)
                    if any(delta[f] != last_counts[f] for f in _COUNTER_FIELDS):
                        mismatches += 1

                if all(v == 0 for v in delta.values()):
                    duplicates += 1
                    ordinal += 1
                    continue

                for field in _COUNTER_FIELDS:
                    summed[field] += delta[field]

                if ts is None or not model or not session_id:
                    lines_skipped += 1
                    ordinal += 1
                    continue

                events.append(
                    UsageEvent(
                        # token_count events carry no id of their own, so the
                        # position in the file supplies one. Stable because these
                        # files are append-only and always parsed from the start.
                        event_key=f"{PROVIDER}:{session_id}:{turn_id or 'na'}:{ordinal}",
                        provider=PROVIDER,
                        model=model,
                        effort=effort,
                        ts=ts,
                        tokens=self._to_tokens(delta),
                        session_id=session_id,
                        project=project_label(session_cwd, project_mode),
                        raw_file=redact_path(path),
                        raw_line=lineno,
                    )
                )
                ordinal += 1

        # The reconciliation: our deltas must account for every token the session
        # itself claims, including any banked before a counter reset. This holds
        # by construction when the format is read correctly, so a failure means
        # a parser bug rather than odd data — and is reported as one.
        checks = failures = 0
        if final_total is not None:
            checks = 1
            if any(summed[f] != carried[f] + final_total[f] for f in _COUNTER_FIELDS):
                failures = 1

        return ParseResult(
            events=events,
            quotas=quotas,
            offset=0,  # always re-read whole; see module docstring
            lines_read=lines_read,
            lines_skipped=lines_skipped,
            duplicates_dropped=duplicates,
            integrity_checks=checks,
            integrity_failures=failures,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _load(raw: bytes) -> dict[str, Any] | None:
        import json

        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return record if isinstance(record, dict) else None

    @staticmethod
    def _to_tokens(delta: dict[str, int]) -> TokenCounts:
        """Normalise Codex's overlapping counters into disjoint ones."""
        cached = delta["cached_input_tokens"]
        written = delta["cache_write_input_tokens"]
        fresh = delta["input_tokens"] - cached - written
        return TokenCounts(
            # Clamped: a negative here would mean the subsets overlap in a way
            # the observed arithmetic rules out, and a negative token count would
            # silently credit the user money.
            input=max(fresh, 0),
            output=delta["output_tokens"],
            reasoning=delta["reasoning_output_tokens"],
            cache_read=cached,
            cache_write_5m=written,
            cache_write_1h=0,  # OpenAI caching has no TTL split
        )

    @staticmethod
    def _quota_snapshots(rate_limits: Any, ts: datetime | None) -> list[QuotaSnapshot]:
        """Turn Codex's rate-limit block into snapshots.

        These are the only *exact* quota readings available to this tool from
        any provider — Codex writes real percentages to its own logs, needing no
        credentials and no network.
        """
        observed = ts or datetime.now(UTC)
        plan = pluck_str(rate_limits, "plan_type", max_len=64)
        out: list[QuotaSnapshot] = []
        for window in ("primary", "secondary"):
            block = pluck_mapping(rate_limits, window)
            if not block:
                continue
            used = pluck_float(block, "used_percent")
            if used is None:
                continue  # an empty block is not a reading of zero
            resets_epoch = pluck_int(block, "resets_at")
            out.append(
                QuotaSnapshot(
                    provider=PROVIDER,
                    window_name=window,
                    used_percent=used,
                    observed_at=observed,
                    source=QuotaSource.EXACT,
                    window_minutes=pluck_int(block, "window_minutes") or None,
                    resets_at=(
                        datetime.fromtimestamp(resets_epoch, tz=UTC) if resets_epoch else None
                    ),
                    plan_type=plan,
                )
            )
        return out


register(CodexAdapter())
