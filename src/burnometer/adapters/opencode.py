"""OpenCode — ``~/.local/share/opencode/opencode.db``.

OpenCode routes to any provider, so one adapter reaches DeepSeek, Kimi, GLM, Qwen
and MiniMax, and is the only route to local models, which record nothing themselves.

Three things about this source differ from every other one we parse, and all three
were found by reading a real database rather than its documentation.

**Reasoning tokens are excluded from ``output``, but billed at the output rate.**
Codex counts reasoning inside output and Claude Code has no separate field at all,
so the habit from both would understate OpenCode. Verified arithmetically: our price
for a real billed session came to 1.87% under the figure OpenCode recorded for
itself, and the gap was exactly its reasoning count at the output rate. Confirmed on
every message by the identity ``input + output + reasoning + cache == total``, which
held 4/4 where ``input + output == total`` held 0/4.

**Its ``cost`` column cannot be trusted at zero.** Three real sessions of the same
tool produced ``0.0`` for a free model, ``0.0`` for a ChatGPT subscription that spent
real tokens, and ``0.00457125`` for the same model on an API key. Zero therefore
means either "free" or "not billed per token", and reading it as money spent would
reproduce exactly the ``$0.00`` failure this project exists to avoid. Tokens are
priced from our own catalog; OpenCode's figure is used only as a cross-check when it
is non-zero.

**Credentials live inside the database being read.** ``account``,
``control_account`` and ``credential`` hold access and refresh tokens, and ``part``
holds conversation text — all in the same file as the usage. A filename deny-list
cannot help when the secret is a column in the next table, so every query here names
its columns and touches only ``session`` and ``message``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..models import TokenCounts, UsageEvent
from ..safety import AdapterError, assert_within, project_label, redact, redact_path
from .base import LogSource, ParseResult, register, resolve_roots

PROVIDER = "opencode"
ENV_VAR = "OPENCODE_DATA"
DEFAULT_ROOTS = (
    Path.home() / ".local/share/opencode",
    Path.home() / "Library/Application Support/opencode",
)
DB_NAME = "opencode.db"

# The only tables this adapter may read. Named explicitly so that adding a table to
# the query is a visible decision rather than a side effect of `SELECT *`.
READABLE_TABLES = frozenset({"session", "message"})


class OpenCodeAdapter:
    name = PROVIDER
    display_name = "OpenCode"
    implemented = True
    # The database is rewritten in place as sessions grow, so byte offsets mean
    # nothing here; every scan reads it whole and relies on event-key dedup.
    rescan_unchanged = True

    def sources(self) -> Sequence[LogSource]:
        """The single database file, named directly.

        Never a glob over the directory: ``auth.json`` sits beside it and holds the
        provider credential.
        """
        return [
            LogSource(root=root, glob=DB_NAME, env_var=ENV_VAR)
            for root in resolve_roots(ENV_VAR, DEFAULT_ROOTS)
        ]

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        assert_within(root, path)
        events: list[UsageEvent] = []
        checks = failures = 0

        try:
            # Read-only and immutable=0: OpenCode may be running and writing.
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise AdapterError(redact_path(path), 0, redact(str(exc))) from None

        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            sessions = self._read_sessions(conn, path)
            events = self._read_messages(conn, path, sessions, project_mode)
            checks, failures = self._reconcile(sessions, events)
        except sqlite3.Error as exc:
            raise AdapterError(redact_path(path), 0, redact(str(exc))) from None
        finally:
            conn.close()

        return ParseResult(
            events=events,
            quotas=[],
            offset=0,
            integrity_checks=checks,
            integrity_failures=failures,
        )

    # ---------------------------------------------------------------- reading

    def _read_sessions(self, conn: sqlite3.Connection, path: Path) -> dict[str, dict]:
        """Session rows, for project attribution and the reconciliation check."""
        out: dict[str, dict] = {}
        for sid, model, directory, cost, ti, to, tr, tcr, tcw in conn.execute(
            "SELECT id, model, directory, cost, tokens_input, tokens_output, "
            "tokens_reasoning, tokens_cache_read, tokens_cache_write FROM session"
        ):
            out[sid] = {
                "model": self._model_slug(model),
                "directory": directory,
                "cost": cost,
                "totals": (ti or 0, to or 0, tr or 0, tcr or 0, tcw or 0),
            }
        return out

    def _read_messages(
        self,
        conn: sqlite3.Connection,
        path: Path,
        sessions: dict[str, dict],
        project_mode: str,
    ) -> list[UsageEvent]:
        events: list[UsageEvent] = []
        for mid, sid, data, created in conn.execute(
            "SELECT id, session_id, data, time_created FROM message ORDER BY time_created"
        ):
            try:
                d = json.loads(data) if data else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("role") != "assistant":
                continue

            tokens = d.get("tokens")
            if not isinstance(tokens, dict):
                continue

            # The bare model id, matching the catalog. Prefixing it with the
            # upstream provider would be more descriptive and would price nothing:
            # Catalog.get is exact-match by design, because guessing a rate is
            # worse than reporting none. The upstream provider is kept out of the
            # slug for the same reason Claude Code stores `claude-opus-5` rather
            # than `anthropic/claude-opus-5`.
            model = d.get("modelID") or self._nested_model(d) or "unknown"
            session = sessions.get(sid, {})

            events.append(
                UsageEvent(
                    event_key=f"{PROVIDER}:{sid}:{mid}",
                    provider=PROVIDER,
                    model=model,
                    effort=None,
                    ts=self._timestamp(d, created),
                    tokens=self._to_tokens(tokens),
                    session_id=sid,
                    project=project_label(session.get("directory"), project_mode),
                    raw_file=redact_path(path),
                    raw_line=0,
                )
            )
        return events

    # ---------------------------------------------------------------- mapping

    @staticmethod
    def _to_tokens(tokens: dict) -> TokenCounts:
        """Map OpenCode's shape onto ours, folding reasoning into output.

        This is the whole point of the adapter. ``TokenCounts.reasoning`` is
        display-only and excluded from ``.total``, which is right for sources that
        already count it inside output. OpenCode does not, so leaving it out here
        would drop tokens that were genuinely billed.
        """
        cache = tokens.get("cache") or {}

        def n(value: object) -> int:
            return value if isinstance(value, int) and value >= 0 else 0

        reasoning = n(tokens.get("reasoning"))
        return TokenCounts(
            input=n(tokens.get("input")),
            output=n(tokens.get("output")) + reasoning,
            reasoning=reasoning,
            cache_read=n(cache.get("read")),
            cache_write_5m=n(cache.get("write")),
            cache_write_1h=0,
        )

    @staticmethod
    def _model_slug(model: object) -> str:
        """`session.model` is a JSON blob, not a string."""
        if isinstance(model, str) and model.startswith("{"):
            try:
                d = json.loads(model)
            except ValueError:
                return model
            provider, mid = d.get("providerID"), d.get("id") or d.get("modelID")
            return f"{provider}/{mid}" if provider and mid else (mid or "unknown")
        return model if isinstance(model, str) else "unknown"

    @staticmethod
    def _nested_model(d: dict) -> str | None:
        m = d.get("model")
        return m.get("modelID") if isinstance(m, dict) else None

    @staticmethod
    def _timestamp(d: dict, fallback: int | None) -> datetime:
        """Milliseconds since epoch, from the message or the row.

        Returns a datetime, not a string: the store formats it, and handing it a
        string fails deep inside the writer rather than here.
        """
        t = d.get("time")
        ms = None
        if isinstance(t, dict):
            ms = t.get("completed") or t.get("created")
        if not isinstance(ms, int):
            ms = fallback if isinstance(fallback, int) else None
        if ms is None:
            return datetime.now(UTC)
        return datetime.fromtimestamp(ms / 1000, UTC)

    # ---------------------------------------------------------- reconciliation

    @staticmethod
    def _reconcile(sessions: dict[str, dict], events: list[UsageEvent]) -> tuple[int, int]:
        """Summed messages must equal the session's own totals.

        Free where the format provides it, like Codex's running total: a format
        change surfaces as a failed check rather than a quietly wrong number.
        """
        summed: dict[str, list[int]] = {}
        for e in events:
            acc = summed.setdefault(e.session_id or "", [0, 0, 0, 0, 0])
            t = e.tokens
            acc[0] += t.input
            # Session totals keep reasoning separate, so subtract what we folded in.
            acc[1] += t.output - t.reasoning
            acc[2] += t.reasoning
            acc[3] += t.cache_read
            acc[4] += t.cache_write_5m + t.cache_write_1h

        checks = failures = 0
        for sid, session in sessions.items():
            if sid not in summed:
                continue
            checks += 1
            if tuple(summed[sid]) != session["totals"]:
                failures += 1
        return checks, failures


register(OpenCodeAdapter())
