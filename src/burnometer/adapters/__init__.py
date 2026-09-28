"""Provider adapters.

Importing this package registers every adapter. Discovery lives here rather than
in ``base`` so the module defining the protocol does not import its own
implementations — that cycle worked only because the import was deferred inside
a function, and a cycle that works by accident is one edit away from not.
"""

# Imported for their registration side effect. Order sets the order `doctor`
# lists them in.
from . import (  # noqa: F401,E402  (side effect)
    claude_code,
    claude_desktop,
    claude_statusline,
    codex,
    kimi,
    opencode,
)
from .base import REGISTRY, Adapter, LogSource, ParseResult, register, registered

__all__ = [
    "Adapter",
    "LogSource",
    "ParseResult",
    "REGISTRY",
    "register",
    "registered",
    "get_adapters",
]


def get_adapters() -> list[Adapter]:
    """All registered adapters."""
    return registered()
