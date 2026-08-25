"""The SQLite store.

This file is also the engine/UI boundary: the menu bar app opens the very same
database read-only. That is why :meth:`Store.open` supports ``read_only=True``
and enforces it at the driver level with a ``mode=ro`` URI — a UI bug then
*cannot* corrupt the store, rather than merely being expected not to.

Security posture (G4):
  * the database, its ``-wal``/``-shm`` sidecars and the containing directory
    are forced to ``0600``/``0700`` immediately after creation, so there is no
    window in which they are world-readable;
  * ``PRAGMA trusted_schema=OFF`` blocks a tampered schema from invoking
    functions during ordinary queries;
  * every statement in this module is parameterised. There is no string
    interpolation into SQL anywhere, and a test asserts that.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import CostBasis, QuotaSnapshot, TokenCounts, UsageEvent
from ..safety import harden_path, secure_dir

SCHEMA_VERSION = 2
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

__all__ = ["Store", "ScanState", "SCHEMA_VERSION"]


@dataclass(slots=True)
class ScanState:
    """Where we stopped reading a file last time.

    Identified by ``path_key`` (a hash) rather than the path, so the store never
    records which directories the user works in. See :func:`burnometer.safety.path_key`.
    """

    path_key: str
    path_label: str | None = None
    inode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    offset: int = 0
    last_scan: str | None = None

    def is_stale_for(self, st: Any) -> bool:
        """True if the file must be re-read from byte zero.

        Covers rotation (inode changed) and truncation (file is now shorter
        than where we stopped). Growth alone is not stale — that is the normal
        incremental case.
        """
        if self.inode is not None and st.st_ino != self.inode:
            return True
        return st.st_size < self.offset


def _iso(dt: datetime) -> str:
    """Serialise to ISO-8601 in UTC, so lexical order equals chronological."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema.

    ``CREATE TABLE IF NOT EXISTS`` never alters a table that already exists, so a
    new column has to be added explicitly or every upgrade silently keeps the old
    shape and fails on the first insert.

    Migrations are keyed on ``PRAGMA user_version`` and must be idempotent: this
    runs on every open, including on a database that is already current.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return

    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "usage_events" not in have:
        return  # a fresh database; the schema script creates it correctly

    # v1 -> v2: upstream_provider, so a router's events can say who served them.
    if version < 2:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(usage_events)")}
        if "upstream_provider" not in columns:
            conn.execute("ALTER TABLE usage_events ADD COLUMN upstream_provider TEXT")

        # Rows stored before this column existed cannot be repaired in place:
        # upsert_events is DO NOTHING by design, so a rescan will not update them.
        # Only OpenCode rows can carry the field, so only those are dropped, and
        # the next scan rebuilds them with it set. Nothing is lost - every row is
        # derived from logs the provider still has, which is why `reset --purge`
        # is safe for the same reason.
        #
        # Scan offsets are deliberately left alone. OpenCode sets
        # rescan_unchanged, so its database is re-read on every scan regardless of
        # offsets; clearing them would force a full re-parse of Claude Code and
        # Codex too, for no benefit.
        conn.execute("DELETE FROM usage_events WHERE provider = 'opencode'")

    conn.commit()


class Store:
    """Owns the connection. Construct via :meth:`open`."""

    def __init__(self, conn: sqlite3.Connection, path: Path, read_only: bool) -> None:
        self._conn = conn
        self.path = path
        self.read_only = read_only

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str, *, read_only: bool = False) -> Store:
        p = Path(path)

        if read_only:
            if not p.exists():
                raise FileNotFoundError(f"no database at {p}")
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        else:
            secure_dir(p.parent)
            conn = sqlite3.connect(p)

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

        if not read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _migrate(conn)
            conn.executescript(_SCHEMA_PATH.read_text())
            # sql-audited: PRAGMA cannot take a bound parameter; the value is
            # our own int constant, asserted below, never user input.
            assert isinstance(SCHEMA_VERSION, int)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")  # sql-audited
            conn.commit()
            # WAL sidecars are created with the process umask; tighten all three.
            for suffix in ("", "-wal", "-shm"):
                harden_path(Path(str(p) + suffix))

        return cls(conn, p, read_only)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- usage events ------------------------------------------------------

    def upsert_events(self, events: Iterable[UsageEvent]) -> int:
        """Insert events, ignoring any whose ``event_key`` is already stored.

        Returns the number of genuinely new rows. Dedup lives in the primary
        key, so a rescan of unchanged files is a no-op and repeated scans are
        idempotent by construction rather than by careful bookkeeping.

        Note the conflict clause targets ``event_key`` specifically rather than
        using ``INSERT OR IGNORE``. ``OR IGNORE`` would also swallow CHECK
        violations, so a row claiming to be both priced and unpriced would
        vanish silently instead of raising — precisely the class of quiet data
        loss this tool exists to avoid.
        """
        rows = [self._event_row(e) for e in events]
        if not rows:
            return 0
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO usage_events (
                    event_key, provider, session_id, project, git_branch,
                    model, model_family, effort, upstream_provider, ts,
                    input_tokens, output_tokens, reasoning_tokens,
                    cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                    cost_usd, cost_basis, price_source, raw_file, raw_line
                ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?,?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                rows,
            )
        return self._conn.total_changes - before

    @staticmethod
    def _event_row(e: UsageEvent) -> tuple[Any, ...]:
        t: TokenCounts = e.tokens
        basis = e.cost_basis.value if isinstance(e.cost_basis, CostBasis) else str(e.cost_basis)
        return (
            e.event_key,
            e.provider,
            e.session_id,
            e.project,
            e.git_branch,
            e.model,
            e.model_family,
            e.effort,
            e.upstream_provider,
            _iso(e.ts),
            t.input,
            t.output,
            t.reasoning,
            t.cache_read,
            t.cache_write_5m,
            t.cache_write_1h,
            e.cost_usd,
            basis,
            e.price_source,
            e.raw_file,
            e.raw_line,
        )

    def iter_priceable(self) -> Iterable[tuple[str, str, str, TokenCounts]]:
        """Yield ``(event_key, provider, model, tokens)`` for every stored event.

        Used by ``reprice``: stored costs are a cache of the best answer at scan
        time, not a permanent verdict. When rates change or the overlay gains a
        model, they must be recomputed rather than left stale.
        """
        for row in self._conn.execute(
            "SELECT event_key, provider, model, input_tokens, output_tokens, "
            "reasoning_tokens, cache_read_tokens, cache_write_5m_tokens, "
            "cache_write_1h_tokens FROM usage_events"
        ):
            yield (
                row["event_key"],
                row["provider"],
                row["model"],
                TokenCounts(
                    input=row["input_tokens"],
                    output=row["output_tokens"],
                    reasoning=row["reasoning_tokens"],
                    cache_read=row["cache_read_tokens"],
                    cache_write_5m=row["cache_write_5m_tokens"],
                    cache_write_1h=row["cache_write_1h_tokens"],
                ),
            )

    def update_costs(
        self, rows: Iterable[tuple[str, float | None, CostBasis | str, str | None]]
    ) -> int:
        """Apply recomputed costs. ``rows`` is ``(event_key, usd, basis, source)``."""
        payload = [
            (
                usd,
                basis.value if isinstance(basis, CostBasis) else str(basis),
                source,
                key,
            )
            for key, usd, basis, source in rows
        ]
        if not payload:
            return 0
        with self._conn:
            self._conn.executemany(
                "UPDATE usage_events SET cost_usd = ?, cost_basis = ?, price_source = ? "
                "WHERE event_key = ?",
                payload,
            )
        return len(payload)

    def prune(
        self,
        *,
        quota_before: str | None = None,
        events_before: str | None = None,
    ) -> dict[str, int]:
        """Delete history older than the given ISO timestamps.

        Bounds a database that would otherwise grow forever: Codex emits a quota
        reading per turn and Claude's desktop app writes one every 15 minutes, so
        readings accumulate far faster than usage. Nothing here is unrecoverable
        — a `scan --force` rebuilds everything from the provider logs.
        """
        removed = {"quota_snapshots": 0, "usage_events": 0}
        with self._conn:
            if quota_before:
                cur = self._conn.execute(
                    "DELETE FROM quota_snapshots WHERE observed_at < ?", (quota_before,)
                )
                removed["quota_snapshots"] = cur.rowcount or 0
            if events_before:
                cur = self._conn.execute("DELETE FROM usage_events WHERE ts < ?", (events_before,))
                removed["usage_events"] = cur.rowcount or 0
        return removed

    def cost_summary(self) -> list[sqlite3.Row]:
        """Per-model totals, grouped by cost basis so the two never fuse."""
        return self._conn.execute(
            """
            SELECT provider, model, model_family, cost_basis,
                   COUNT(*) AS requests,
                   SUM(cost_usd) AS cost_usd,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_5m_tokens) AS cache_write_5m_tokens,
                   SUM(cache_write_1h_tokens) AS cache_write_1h_tokens,
                   MAX(price_source) AS price_source
            FROM usage_events
            GROUP BY provider, model, cost_basis
            ORDER BY cost_usd DESC NULLS LAST, requests DESC
            """
        ).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a read query built inside this package.

        ``sql`` is always constructed from module-level constants and whitelists
        — never from caller input — and every value is bound. See
        ``tests/test_security.py::test_all_sql_is_parameterised``.
        """
        return self._conn.execute(sql, params).fetchall()

    def count_events(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])

    # -- scan state --------------------------------------------------------

    def get_scan_state(self, key: str) -> ScanState | None:
        """Look up scan progress by :func:`~burnometer.safety.path_key`, not by path."""
        row = self._conn.execute(
            "SELECT path_key, path_label, inode, size, mtime_ns, offset, last_scan "
            "FROM scan_state WHERE path_key = ?",
            (key,),
        ).fetchone()
        return ScanState(**dict(row)) if row else None

    def set_scan_state(self, state: ScanState) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scan_state (
                    path_key, path_label, inode, size, mtime_ns, offset, last_scan
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(path_key) DO UPDATE SET
                    path_label=excluded.path_label, inode=excluded.inode,
                    size=excluded.size, mtime_ns=excluded.mtime_ns,
                    offset=excluded.offset, last_scan=excluded.last_scan
                """,
                (
                    state.path_key,
                    state.path_label,
                    state.inode,
                    state.size,
                    state.mtime_ns,
                    state.offset,
                    state.last_scan or _iso(datetime.now(UTC)),
                ),
            )

    # -- quota -------------------------------------------------------------

    def record_quota(self, snapshots: Iterable[QuotaSnapshot]) -> int:
        rows = [
            (
                s.provider,
                s.window_name,
                s.used_percent,
                s.window_minutes,
                _iso(s.resets_at) if s.resets_at else None,
                s.plan_type,
                _iso(s.observed_at),
                s.source.value,
            )
            for s in snapshots
        ]
        if not rows:
            return 0
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO quota_snapshots (
                    provider, window_name, used_percent, window_minutes,
                    resets_at, plan_type, observed_at, source
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(provider, window_name, observed_at) DO UPDATE SET
                    -- A reset time is derived from the series, so a reading
                    -- already stored can gain one on a later scan once enough
                    -- history exists to work it out. COALESCE fills a gap and
                    -- never erases a value we already had.
                    resets_at = COALESCE(excluded.resets_at, quota_snapshots.resets_at),
                    plan_type = COALESCE(excluded.plan_type, quota_snapshots.plan_type)
                """,
                rows,
            )
        return self._conn.total_changes - before

    def latest_quota(self, provider: str) -> Sequence[sqlite3.Row]:
        """Most recent reading per window for one provider."""
        return self._conn.execute(
            """
            SELECT q.* FROM quota_snapshots q
            JOIN (
                SELECT window_name, MAX(observed_at) AS newest
                FROM quota_snapshots WHERE provider = ? GROUP BY window_name
            ) latest
              ON q.window_name = latest.window_name AND q.observed_at = latest.newest
            WHERE q.provider = ?
            """,
            (provider, provider),
        ).fetchall()

    # -- introspection for `doctor` ---------------------------------------

    def stats(self) -> dict[str, Any]:
        c = self._conn
        return {
            "schema_version": self.schema_version,
            "events": self.count_events(),
            "files_tracked": int(c.execute("SELECT COUNT(*) FROM scan_state").fetchone()[0]),
            "quota_readings": int(c.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0]),
            "providers": [
                r[0] for r in c.execute("SELECT DISTINCT provider FROM usage_events ORDER BY 1")
            ],
            "models": [
                r[0] for r in c.execute("SELECT DISTINCT model FROM usage_events ORDER BY 1")
            ],
            "unpriced": int(
                c.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE cost_basis = 'unpriced'"
                ).fetchone()[0]
            ),
            # Counted apart from unpriced on purpose: one means the rate is
            # unknown, the other that no rate exists.
            "not_metered": int(
                c.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE cost_basis = 'not_metered'"
                ).fetchone()[0]
            ),
            "earliest": c.execute("SELECT MIN(ts) FROM usage_events").fetchone()[0],
            "latest": c.execute("SELECT MAX(ts) FROM usage_events").fetchone()[0],
        }
