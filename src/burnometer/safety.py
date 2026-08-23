"""Security guarantees, enforced in code rather than documented in prose.

This tool reads the most sensitive files on a developer's machine — complete
conversation transcripts containing prompts, completions, pasted source and
occasionally secrets — and a credential store sits *inside* one of the
directories it scans (``~/.codex/auth.json``). The guarantees below exist
because "be careful" is not a control.

G1  Content firewall
    Message content never enters the process beyond the JSON parse and never
    leaves it at all. Adapters extract via the allowlists here, using the
    ``pluck_*`` helpers, which return scalars only. A dict or list can never be
    lifted out of a parsed record, so there is no path by which prompt text
    reaches the database.

G2  Credential isolation
    Three independent layers, any one of which suffices: narrow globs in the
    adapters, :func:`is_credential_path` immediately before every open, and
    :func:`assert_within` to stop a symlink escaping its expected root.

G4  Filesystem posture
    :func:`secure_dir` and :func:`secure_open_write` create ``0700`` / ``0600``
    from the first byte, so there is never a world-readable window.

G6  Error hygiene
    :func:`redact` and :class:`AdapterError` guarantee a malformed line is
    reported as a location and a length — never as content. Line one of a
    transcript can be an API key.

See ``SECURITY.md`` for the full threat model.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

__all__ = [
    "SecurityError",
    "CredentialAccessBlocked",
    "PathEscapeBlocked",
    "UnsafeFileType",
    "AdapterError",
    "USAGE_FIELDS",
    "METADATA_FIELDS",
    "CONTENT_KEYS",
    "CREDENTIAL_FILENAMES",
    "CREDENTIAL_SUFFIXES",
    "is_credential_path",
    "is_credential_key",
    "select_keys",
    "assert_within",
    "open_log_readonly",
    "secure_dir",
    "secure_open_write",
    "harden_path",
    "redact",
    "redact_path",
    "project_label",
    "path_key",
    "pluck_int",
    "pluck_str",
    "pluck_float",
    "pluck_mapping",
]


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class SecurityError(Exception):
    """Base class for a refused operation. Never carries file content."""


class CredentialAccessBlocked(SecurityError):
    """A path matched the credential deny-list and was not opened."""


class PathEscapeBlocked(SecurityError):
    """A path resolved outside the root it was discovered under."""


class UnsafeFileType(SecurityError):
    """Target is not a regular file (symlink, FIFO, device, directory)."""


class AdapterError(Exception):
    """A parse failure, carrying a location but never the offending bytes.

    Adapters catch every exception at their boundary and re-raise as this, with
    the original detached, so an unhandled traceback can never print a fragment
    of a transcript to a terminal or into a GitHub issue.
    """

    def __init__(self, path: Path | str, lineno: int, reason: str) -> None:
        self.path = str(path)
        self.lineno = lineno
        self.reason = reason
        super().__init__(f"{self.path}:{lineno}: {reason}")


# --------------------------------------------------------------------------
# G1 — Content firewall: field allowlists
# --------------------------------------------------------------------------

USAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # Claude Code
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
        # Codex
        "cached_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }
)
"""Numeric usage fields an adapter may read. Nothing else is token data."""

METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "requestId",
        "id",
        "model",
        "timestamp",
        "sessionId",
        "cwd",
        "gitBranch",
        "turn_id",
        "effort",
        "plan_type",
        "used_percent",
        "window_minutes",
        "resets_at",
        "limit_id",
    }
)
"""Non-token fields an adapter may read. Deliberately excludes anything that
can carry free text: no ``content``, ``text``, ``summary``, ``slug``,
``aiTitle``, ``base_instructions`` or ``lastPrompt``."""

CONTENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "content",
        "text",
        "base_instructions",
        "instructions_template",
        "lastPrompt",
        "aiTitle",
        "slug",
        "summary",
        "description",
        "arguments",
        "input",
        "output",
        "message",
        "payload",
        "toolUseResult",
        "snapshot",
        "diagnostics",
    }
)
"""Keys known to carry free text or nested payloads. Never extracted. Used by
``tests/test_security.py`` to assert none of them reach the store."""


# --------------------------------------------------------------------------
# G2 — Credential isolation
# --------------------------------------------------------------------------

CREDENTIAL_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "auth.json",
        "oauth_creds.json",
        "credentials.json",
        ".credentials.json",
        "token.json",
        "tokens.json",
        ".codex-global-state.json",
        "google_accounts.json",
        "installation_id",
        "id_token",
        ".netrc",
        ".htpasswd",
    }
)

CREDENTIAL_SUFFIXES: Final[tuple[str, ...]] = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".keychain",
    ".keystore",
    ".env",
    ".ppk",
)

SENSITIVE_DIRS: Final[frozenset[str]] = frozenset(
    {".ssh", ".gnupg", ".aws", ".kube", ".docker", "Keychains"}
)


def is_credential_path(path: Path | str) -> bool:
    """True if ``path`` looks like a credential store and must never be opened.

    Matching is on name and suffix, case-insensitively, plus any component that
    is a well-known secret directory. This runs immediately before every open,
    independent of how the path was discovered.
    """
    p = Path(path)
    name = p.name.lower()
    if name in CREDENTIAL_FILENAMES:
        return True
    if name.endswith(CREDENTIAL_SUFFIXES):
        return True
    return any(part in SENSITIVE_DIRS for part in p.parts)


#: Key names that carry credentials or account identity inside a key-value
#: store. Matched as substrings, case-insensitively, because these stores use
#: long dotted names like ``someProduct.unifiedStateSync.oauthToken``.
CREDENTIAL_KEY_MARKERS: Final[tuple[str, ...]] = (
    "oauthtoken",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "bearer",
    "apikey",
    "api_key",
    "secret",
    "password",
    "passwd",
    "credential",
    "authstatus",
    "session",
    "cookie",
    "privatekey",
    "signature",
)

#: Key names that carry the account holder's identity. Not credentials, but
#: nothing a usage meter needs, and every one stored is a liability.
IDENTITY_KEY_MARKERS: Final[tuple[str, ...]] = (
    "email",
    "username",
    "userstatus",
    "userinfo",
    "profile",
    "account",
    "displayname",
    "avatar",
)


def is_credential_key(key: str) -> bool:
    """True if a key-value entry should never be read.

    File-level checks are not enough for stores that keep a credential *beside*
    the data — a VS Code-style ``state.vscdb`` holds an OAuth token, the account
    holder's name and email, and application state in one table, so opening the
    file is legitimate while reading one of its rows is not.

    This is the second line. The first is :func:`select_keys`: never enumerate
    such a store, name what you want.
    """
    if not key:
        return False
    flat = key.lower().replace("-", "").replace("_", "").replace(".", "")
    return any(m.replace("_", "") in flat for m in CREDENTIAL_KEY_MARKERS + IDENTITY_KEY_MARKERS)


def select_keys(available: Iterable[str], allowed: Iterable[str]) -> list[str]:
    """Intersect what a store offers with what an adapter asked for.

    The correct way to read a key-value store that also holds secrets: state the
    keys you need and take only those. Enumerating and filtering is the wrong
    shape — a new key added upstream is then read by default, and the deny-list
    has to have anticipated it.

    Anything in ``allowed`` that trips :func:`is_credential_key` is dropped and
    reported, so a mistake in an adapter fails loudly rather than silently
    widening what gets read.
    """
    wanted = set(allowed)
    unsafe = {k for k in wanted if is_credential_key(k)}
    if unsafe:
        raise CredentialAccessBlocked(f"adapter asked for credential-shaped keys: {sorted(unsafe)}")
    return sorted(set(available) & wanted)


def assert_within(root: Path | str, path: Path | str) -> Path:
    """Resolve ``path`` and verify it stays inside ``root``.

    Both sides are fully resolved first, so this defeats a symlink pointing out
    of the tree (``sessions/foo.jsonl -> ~/.codex/auth.json``) and tolerates a
    root that is itself a symlink, which is normal on macOS where ``/Users``
    resolves through ``/System/Volumes/Data``.

    Returns the resolved path. Raises :class:`PathEscapeBlocked` otherwise.
    """
    root_r = Path(root).resolve()
    path_r = Path(path).resolve()
    if not path_r.is_relative_to(root_r):
        raise PathEscapeBlocked(
            f"{redact_path(path)} resolves outside its expected root {redact_path(root)}"
        )
    return path_r


def open_log_readonly(root: Path | str, path: Path | str):
    """Open a provider log file for reading, with every G2 check applied.

    Order matters: containment, then deny-list, then an ``O_NOFOLLOW`` open so a
    symlink swapped in between the check and the open still fails, then a
    regular-file assertion so a FIFO cannot block us or a device be read.

    Returns a binary file object. The caller must close it.
    """
    resolved = assert_within(root, path)
    if is_credential_path(path) or is_credential_path(resolved):
        raise CredentialAccessBlocked(f"refused to open credential file {redact_path(path)}")

    # Check the file type BEFORE opening. Opening a FIFO blocks indefinitely
    # waiting for a writer, so a hostile named pipe placed in a scanned tree
    # would otherwise hang the scanner — a denial of service that costs an
    # attacker one mkfifo. lstat also rejects a symlink without following it.
    lst = os.lstat(path)
    if not stat.S_ISREG(lst.st_mode):
        raise UnsafeFileType(
            f"{redact_path(path)} is not a regular file (mode {stat.filemode(lst.st_mode)})"
        )

    # O_NOFOLLOW closes the swap-after-check window; O_NONBLOCK is belt and
    # braces in case the entry is replaced by a FIFO between lstat and open.
    fd = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeFileType(f"{redact_path(path)} is not a regular file")
        # Regular-file reads ignore O_NONBLOCK, but clear it so nothing
        # downstream sees a short read and mistakes it for EOF.
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


# --------------------------------------------------------------------------
# G4 — Filesystem posture
# --------------------------------------------------------------------------


def secure_dir(path: Path | str) -> Path:
    """Create (or tighten) a directory to ``0700`` and return it."""
    p = Path(path)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(p.stat().st_mode) != 0o700:
        p.chmod(0o700)
    return p


def secure_open_write(path: Path | str, *, append: bool = False):
    """Create/open a file for writing with ``0600`` from the very first byte."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        harden_path(path)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "ab" if append else "wb")


def harden_path(path: Path | str) -> None:
    """Force ``0600`` on an existing file, if it exists.

    SQLite creates its own ``-wal`` and ``-shm`` sidecars with the process umask,
    so the store calls this on all three after opening.
    """
    p = Path(path)
    if p.exists() and stat.S_IMODE(p.stat().st_mode) != 0o600:
        p.chmod(0o600)


# --------------------------------------------------------------------------
# G6 — Error hygiene
# --------------------------------------------------------------------------


def redact(value: Any) -> str:
    """Describe a value without disclosing it.

    Used anywhere a value could reach a log, an exception message or a terminal.
    """
    if value is None:
        return "<none>"
    if isinstance(value, (bytes, bytearray)):
        return f"<redacted {len(value)} bytes>"
    if isinstance(value, str):
        return f"<redacted {len(value)} chars>"
    if isinstance(value, (int, float, bool)):
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return f"<mapping, {len(value)} keys>"
    if isinstance(value, (list, tuple, set)):
        return f"<{type(value).__name__}, {len(value)} items>"
    return f"<{type(value).__name__}>"


def redact_path(path: Path | str) -> str:
    """Render a path with the user's home directory replaced by ``~``.

    Keeps error messages useful for debugging without pasting a username, and
    therefore an employer or client name, into a bug report.
    """
    p = Path(path)
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# G1 — Scalar extraction primitives
# --------------------------------------------------------------------------
#
# These are the ONLY sanctioned way to lift a value out of a parsed record.
# Each returns a scalar or a default; none can return a dict, list or arbitrary
# object, so nested content cannot be carried downstream even by accident.


def pluck_int(src: Mapping[str, Any] | None, key: str, default: int = 0) -> int:
    """Return a non-negative int, or ``default``. Non-numeric input is ignored."""
    if not isinstance(src, Mapping):
        return default
    v = src.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    i = int(v)
    return i if i >= 0 else default


def pluck_float(src: Mapping[str, Any] | None, key: str) -> float | None:
    """Return a float, or ``None``. Non-numeric input is ignored."""
    if not isinstance(src, Mapping):
        return None
    v = src.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def pluck_str(
    src: Mapping[str, Any] | None,
    key: str,
    *,
    max_len: int = 512,
) -> str | None:
    """Return a length-capped string, or ``None``.

    ``max_len`` is a containment measure, not a formatting one: every field we
    legitimately read (a model slug, a request id, a path, a branch) is short.
    A value longer than the cap indicates we are reading the wrong field, so it
    is truncated rather than propagated whole.
    """
    if not isinstance(src, Mapping):
        return None
    v = src.get(key)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return None
    return v[:max_len]


def pluck_mapping(src: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    """Return a nested mapping for further scalar extraction, or ``None``.

    Permitted only for structural descent into known-numeric containers such as
    ``message.usage`` or ``info.last_token_usage``. The returned mapping must be
    consumed with the ``pluck_*`` helpers; it must never be stored or copied.
    """
    if not isinstance(src, Mapping):
        return None
    v = src.get(key)
    return v if isinstance(v, Mapping) else None


# --------------------------------------------------------------------------
# G5 — Project path redaction
# --------------------------------------------------------------------------

PROJECT_PATH_MODES: Final[tuple[str, ...]] = ("full", "basename", "hash", "none")


def project_label(cwd: str | None, mode: str = "basename") -> str | None:
    """Reduce a working directory to the amount of identity the user allows.

    ``cwd`` is the only genuinely identifying string we store, and it is worth
    storing because per-project breakdowns are one of the most useful views. So
    it is controllable rather than dropped, and the reduction happens *here*, in
    the adapter, before the value ever reaches the database — switching to
    ``hash`` therefore leaves nothing recoverable rather than merely hidden.

    ``full``      ``/Users/alice/work/acme-merger``  — as reported
    ``basename``  ``acme-merger``                    — default; drops the user
                                                       name and directory layout
    ``hash``      ``proj_9f2a1c7e``                  — stable grouping, no
                                                       readable name; safe for
                                                       screenshots and bug reports
    ``none``      ``None``                           — no project dimension
    """
    if cwd is None or mode == "none":
        return None
    if mode == "full":
        return cwd
    if mode == "basename":
        return PurePosixPath(cwd).name or cwd
    if mode == "hash":
        digest = hashlib.blake2b(cwd.encode("utf-8"), digest_size=4).hexdigest()
        return f"proj_{digest}"
    raise ValueError(f"unknown project_paths mode {mode!r}; expected one of {PROJECT_PATH_MODES}")


def path_key(path: Path | str) -> str:
    """A stable, content-free identifier for a file path.

    The scanner needs to remember "have I read this file, and how far", but it
    must not remember *which* file. That matters more than it first appears:
    Claude Code names its project directories after the full working directory
    (``-Users-alice-clients-bigcorp-repo``), so storing the path would put the
    account name and the client straight into the database — precisely what
    :func:`project_label` exists to prevent.

    Files are rediscovered by glob on every scan, so the real path is never
    needed from storage; only a key to look up the offset is.
    """
    absolute = str(Path(path).resolve())
    return hashlib.blake2b(absolute.encode("utf-8"), digest_size=16).hexdigest()
