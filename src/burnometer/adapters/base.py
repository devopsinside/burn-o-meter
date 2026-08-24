"""The provider adapter contract.

Adding a provider is one module plus a registry entry. Nothing else in the
codebase changes: the store, the pricing engine, the analytics and every UI
work from :class:`~burnometer.models.UsageEvent` and
:class:`~burnometer.models.QuotaSnapshot` alone.

An adapter carries two responsibilities that are easy to get wrong and are
therefore stated as obligations rather than left to taste:

1. **Normalise tokens** to the invariants documented on ``TokenCounts`` —
   uncached input only, reasoning as a display-only subset of output, cache
   writes split by TTL. Providers disagree about all three.
2. **Extract through the allowlists** in :mod:`burnometer.safety`, using the
   ``pluck_*`` helpers. Never copy a parsed dict; never read a content field.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import QuotaSnapshot, UsageEvent

__all__ = [
    "LogSource",
    "ParseResult",
    "Adapter",
    "REGISTRY",
    "register",
    "registered",
    "resolve_roots",
]


def resolve_roots(env_var: str | None, defaults: Sequence[Path]) -> list[Path]:
    """Where a tool's data actually lives.

    Two things this exists to prevent, both of which fail *silently* — the user
    sees "not installed" and no error:

    * **Relocated data.** Most agents honour an environment variable for their
      home (``CODEX_HOME``, ``OPENCODE_DATA_DIR``, and so on). A user who set one
      said explicitly where their data is, so it wins outright.
    * **More than one default.** Tools move their data between versions and
      platforms — Claude Code writes to ``~/.claude/projects`` or
      ``~/.config/claude/projects`` depending on how it was installed. Checking
      one and reporting zero for the other is worse than not supporting the tool.

    Returns every candidate that exists, so a machine with data in two places
    gets both rather than whichever was listed first.
    """
    if env_var:
        override = os.environ.get(env_var)
        if override:
            # An explicit setting is not a hint; it is the answer.
            return [Path(override).expanduser()]
    return [p for p in defaults if p.exists()]


@dataclass(frozen=True, slots=True)
class LogSource:
    """Where one provider keeps its logs.

    ``root`` is the containment boundary passed to
    :func:`~burnometer.safety.assert_within`, and ``glob`` is deliberately
    narrow. A recursive walk of ``~/.claude`` or ``~/.codex`` would sweep in
    ``auth.json`` and ``*.key``; these patterns cannot.
    """

    root: Path
    glob: str

    #: The environment variable that relocates this tool's data, recorded so
    #: ``doctor`` can tell a user which knob to turn.
    env_var: str | None = None

    def discover(self) -> Iterator[Path]:
        """Yield candidate log files. Does not open anything."""
        if not self.root.exists():
            return
        yield from sorted(self.root.glob(self.glob))


@dataclass(slots=True)
class ParseResult:
    """What one pass over a file produced."""

    events: list[UsageEvent]
    quotas: list[QuotaSnapshot]
    offset: int
    """Byte offset to resume from next time. Only ever advances past a complete
    line, so a file still being appended to is never half-parsed."""

    lines_read: int = 0
    lines_skipped: int = 0

    integrity_checks: int = 0
    """Cross-checks a provider's own format made possible, and we performed."""

    integrity_failures: int = 0
    """Cross-checks that did not reconcile. Non-zero means our reading of the
    format disagrees with the provider's own totals — a parser bug, not a data
    quirk — so it is surfaced rather than swallowed."""

    duplicates_dropped: int = 0
    """Records the provider wrote more than once, collapsed during this parse.

    Counted explicitly rather than left implicit because it is large and
    surprising — Claude Code repeats roughly 60% of its usage records — and a
    user comparing this tool against a naive sum deserves to see exactly where
    the difference comes from."""


@runtime_checkable
class Adapter(Protocol):
    """Implemented by each provider module."""

    name: str
    display_name: str

    rescan_unchanged: bool
    """True for a source whose output is derived from the whole series rather
    than read off individual lines, so an unchanged file can still yield a new
    answer. Cheap only for small files — do not set it on transcripts."""

    implemented: bool
    """False while an adapter only knows where its logs live. The scanner skips
    it and ``doctor`` reports it as detected-but-not-yet-parsed, rather than the
    tool silently appearing to support a provider it cannot read."""

    def sources(self) -> Sequence[LogSource]:
        """Where this adapter's data lives.

        Globs must be narrow enough to reach only the files being parsed. A
        recursive walk of a provider's directory would sweep in the credential
        stores that sit beside the logs, which is the failure this design exists
        to make impossible rather than merely unlikely.
        """

    def parse(
        self,
        path: Path,
        root: Path,
        offset: int = 0,
        project_mode: str = "basename",
    ) -> ParseResult:
        """Parse ``path`` from byte ``offset``.

        Must open via :func:`~burnometer.safety.open_log_readonly` so the
        credential and containment checks apply, and must raise
        :class:`~burnometer.safety.AdapterError` — never a bare exception that
        could carry a line of transcript into a traceback.

        ``project_mode`` is the ``privacy.project_paths`` setting and is applied
        here, before the value reaches storage, so a stricter setting leaves
        nothing recoverable rather than merely hidden.
        """


REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    """Register an adapter under its ``name``."""
    if adapter.name in REGISTRY:
        raise ValueError(f"adapter {adapter.name!r} is already registered")
    REGISTRY[adapter.name] = adapter
    return adapter


def registered() -> list[Adapter]:
    """Everything currently in the registry.

    Deliberately does not import the concrete adapters: a module that defines
    the protocol should not depend on its own implementations. Discovery lives
    in ``adapters/__init__.py``, which is allowed to know about both.
    """
    return list(REGISTRY.values())
