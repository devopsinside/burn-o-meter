"""Kimi Code — ``~/.kimi-code/sessions/*/*/agents/*/wire.jsonl``.

A wire log: one JSON object per line, append-only, recording everything the agent
did. Only one record type is billable, and the two that look billable are not.

**``usage.record`` is the only source of truth.** Each turn also writes
``token_counting.measured`` and ``token_counting.turn_recorded``, both carrying a
``tokens`` field. Neither is a separate measurement — across every turn observed,
``tokens == inputOther + output`` exactly, so they restate the usage record rather
than adding to it. Summing them would roughly double every figure, which is the
same trap Codex's repeated final event set. The identity is cheap to check, so it
ships as the integrity check for this adapter.

**Usage is per turn, not cumulative.** ``usageScope`` reads ``turn``, and a
three-turn session recorded outputs of 276, 239 and 202 — falling, so not a running
total. Summing is therefore correct. Verified rather than assumed, because Codex
looked exactly like this and was cumulative.

**Total input is the sum of three fields.** Kimi computes ``inputOther`` as
``inputTokens - cached``, so cache reads and cache creation sit outside it and the
prompt size is ``inputOther + inputCacheRead + inputCacheCreation``.

**The alias carries the provider.** ``model`` reads ``lmstudio/qwen3:0.6b``: the
segment before the first slash is the provider alias from the user's config, and
the rest is the model id the provider knows. ``lmstudio/openai/gpt-oss-20b``
splits the same way, into ``lmstudio`` and ``openai/gpt-oss-20b``.

**What this adapter must never open.** ``~/.kimi-code/config.toml`` holds
``api_key`` for every configured provider, and inside the wire log itself
``turn.prompt`` and ``context.append_message`` carry the user's prompt text
verbatim. The glob reaches neither the config nor the session ``logs/`` directory,
and the parser reads exactly one record type and names the fields it takes.

A note for anyone reconciling a local model's numbers against expectation: Ollama
truncates a prompt that exceeds ``num_ctx`` and reports the tokens it actually
processed, not the tokens it was sent. A prompt of 8,000 tokens against the
default 4,096-token window is reported as 2,050. That figure is honest — it is
what the model read — and Kimi records it faithfully; it just is not the size of
what you typed. Confirmed directly against Ollama at two window sizes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..models import TokenCounts, UsageEvent
from ..safety import AdapterError, open_log_readonly, project_label, redact_path
from .base import LogSource, ParseResult, register, resolve_roots

PROVIDER = "kimi"
ENV_VAR = "KIMI_CODE_HOME"
DEFAULT_ROOTS = (Path.home() / ".kimi-code",)

#: Exactly four levels down. Not a recursive walk: `config.toml` holds an api_key
#: for every configured provider, and each session keeps a `logs/` directory
#: beside its agents.
SESSION_GLOB = "sessions/*/*/agents/*/wire.jsonl"

#: Maps a session id to the directory it ran in. Holds only sessionId, sessionDir
#: and workDir - no credentials - and is the only file outside the wire logs this
#: adapter opens.
INDEX_NAME = "session_index.jsonl"

#: The only record type carrying billable usage. `token_counting.*` restates it.
USAGE_RECORD = "usage.record"

#: Cheap bytes test before parsing a line as JSON.
_PREFILTER = b'"usage.record"'


class KimiAdapter:
    name = PROVIDER
    display_name = "Kimi Code"
    implemented = True
    # The wire log is append-only, so a file whose size and mtime are unchanged has
    # nothing new in it and byte offsets stay meaningful across scans.
    rescan_unchanged = False

    def sources(self) -> Sequence[LogSource]:
        return [
            LogSource(root=root, glob=SESSION_GLOB, env_var=ENV_VAR)
            for root in resolve_roots(ENV_VAR, DEFAULT_ROOTS)
        ]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        session_id, agent_id = self._identity(path)
        work_dir = self._work_dir(root, session_id)

        events: list[UsageEvent] = []
        seen: set[str] = set()
        lines_read = lines_skipped = duplicates = 0
        checks = failures = 0
        pending: dict | None = None
        pos = offset

        with open_log_readonly(root, path) as fh:
            if offset:
                fh.seek(offset)

            lineno = 0
            for raw in fh:
                lineno += 1

                # A line without a terminator is still being written; stop without
                # advancing past it so the next scan re-reads it whole.
                if not raw.endswith(b"\n"):
                    break
                pos += len(raw)
                lines_read += 1

                # token_counting records are read only to check the usage record
                # they restate, so both prefilters have to pass through here.
                if _PREFILTER not in raw and b'"token_counting.' not in raw:
                    continue

                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    lines_skipped += 1
                    continue
                if not isinstance(record, dict):
                    lines_skipped += 1
                    continue

                kind = record.get("type")
                if kind == "token_counting.measured":
                    if pending is not None:
                        checks += 1
                        if not self._restates(pending, record):
                            failures += 1
                        pending = None
                    continue
                if kind != USAGE_RECORD:
                    continue

                try:
                    event = self._to_event(
                        record, path, lineno, session_id, agent_id, work_dir, project_mode
                    )
                except AdapterError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Detached from the original so no fragment of a prompt can
                    # reach a traceback (G6).
                    raise AdapterError(
                        redact_path(path), lineno, f"{type(exc).__name__} while parsing"
                    ) from None

                if event is None:
                    lines_skipped += 1
                    continue
                if event.event_key in seen:
                    duplicates += 1
                    continue
                seen.add(event.event_key)
                events.append(event)
                pending = record

        return ParseResult(
            events=events,
            quotas=[],  # Kimi Code persists no rate-limit or quota state.
            offset=pos,
            lines_read=lines_read,
            lines_skipped=lines_skipped,
            duplicates_dropped=duplicates,
            integrity_checks=checks,
            integrity_failures=failures,
        )

    # ---------------------------------------------------------------- mapping

    def _to_event(
        self,
        record: dict,
        path: Path,
        lineno: int,
        session_id: str,
        agent_id: str,
        work_dir: str | None,
        project_mode: str,
    ) -> UsageEvent | None:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            return None

        model, upstream = self._split_alias(record.get("model"))
        ts = self._timestamp(record.get("time"))

        # Keyed on the timestamp rather than the line's position in the file:
        # scanning resumes from a byte offset, so a counter would restart at zero
        # on the second scan and collide with the first scan's keys.
        agent = record.get("agentId") or agent_id
        return UsageEvent(
            event_key=f"{PROVIDER}:{session_id}:{agent}:{int(ts.timestamp() * 1000)}",
            provider=PROVIDER,
            model=model,
            upstream_provider=upstream,
            effort=None,
            ts=ts,
            tokens=self._to_tokens(usage),
            session_id=session_id,
            project=project_label(work_dir, project_mode),
            raw_file=redact_path(path),
            raw_line=lineno,
        )

    @staticmethod
    def _to_tokens(usage: dict) -> TokenCounts:
        """Kimi's four fields onto ours.

        ``inputOther`` is already net of cache - Kimi derives it as
        ``inputTokens - cached`` - so it maps straight onto ``input`` without
        subtracting anything here.
        """

        def n(value: object) -> int:
            return value if isinstance(value, int) and value >= 0 else 0

        return TokenCounts(
            input=n(usage.get("inputOther")),
            output=n(usage.get("output")),
            reasoning=0,  # Kimi reports no separate reasoning count.
            cache_read=n(usage.get("inputCacheRead")),
            cache_write_5m=n(usage.get("inputCacheCreation")),
            cache_write_1h=0,
        )

    @staticmethod
    def _split_alias(alias: object) -> tuple[str, str | None]:
        """``lmstudio/qwen3:0.6b`` into the model id and the provider alias.

        Split once, not on every slash: ``lmstudio/openai/gpt-oss-20b`` is the
        ``openai/gpt-oss-20b`` model served by the provider aliased ``lmstudio``,
        which is exactly how the config that defines it is keyed.

        The provider is kept out of the slug because ``Catalog.get`` matches
        exactly, and it is kept at all because it is what separates "no rate is
        published" from "no rate exists, it ran on your own hardware".
        """
        if not isinstance(alias, str) or not alias:
            return "unknown", None
        provider, sep, model = alias.partition("/")
        if not sep or not model:
            return alias, None
        return model, provider

    @staticmethod
    def _restates(usage_record: dict, measured: dict) -> bool:
        """Whether ``token_counting`` agrees with the usage record it follows.

        Not an accounting step - the two are never added. It confirms that the
        fields being read are the ones Kimi itself totals, so a future change to
        the wire format surfaces as a failed check rather than a wrong number.
        """
        usage = usage_record.get("usage")
        if not isinstance(usage, dict):
            return False
        tokens = measured.get("tokens")
        if not isinstance(tokens, int):
            return False

        def n(value: object) -> int:
            return value if isinstance(value, int) and value >= 0 else 0

        return tokens == n(usage.get("inputOther")) + n(usage.get("output"))

    # ---------------------------------------------------------------- context

    @staticmethod
    def _identity(path: Path) -> tuple[str, str]:
        """``.../<session>/agents/<agent>/wire.jsonl`` -> session and agent."""
        agent = path.parent.name
        session = path.parent.parent.parent.name
        return session, agent

    def _work_dir(self, root: Path, session_id: str) -> str | None:
        """The directory a session ran in, from the index beside the sessions.

        The workspace directory name embeds the basename already
        (``wd_myrepo_0797854e9d96``), but only the basename - so the index is read
        instead, and ``project_paths = "full"`` or ``"hash"`` get a real path to
        work from rather than a slug that cannot produce one.
        """
        index = root / INDEX_NAME
        try:
            stamp = index.stat().st_mtime_ns
        except OSError:
            return None
        if getattr(self, "_index_stamp", None) != (index, stamp):
            self._index = self._read_index(index)
            self._index_stamp = (index, stamp)
        return self._index.get(session_id)

    @staticmethod
    def _read_index(index: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            with open(index, "rb") as fh:
                for raw in fh:
                    try:
                        d = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(d, dict):
                        sid, wd = d.get("sessionId"), d.get("workDir")
                        if isinstance(sid, str) and isinstance(wd, str):
                            out[sid] = wd
        except OSError:
            return {}
        return out

    @staticmethod
    def _timestamp(ms: object) -> datetime:
        """Milliseconds since epoch, as a datetime.

        A datetime and not a string: the store formats it, and handing it a string
        fails deep inside the writer rather than here.
        """
        if isinstance(ms, int) and ms > 0:
            return datetime.fromtimestamp(ms / 1000, tz=UTC)
        return datetime.now(tz=UTC)


register(KimiAdapter())
