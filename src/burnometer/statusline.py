"""Claude Code's live rate-limit figures, captured through its status line.

The Claude desktop app's plan records (see ``adapters/claude_desktop.py``) are
exact but not live: the app writes a sample only when it fetches one, and a
reading can sit unchanged for an hour while Claude Code works through a window.
Measured on real data, a five-hour window read 0% for over an hour after a fresh
one had opened under Claude Code use.

Claude Code has the current figure on every turn - the service reports it on
each response - and hands it to the configured status line command on stdin:

    {"rate_limits": {"five_hour": {"used_percentage": 34.5, "resets_at": ...},
                     "seven_day": {"used_percentage": 15.0, "resets_at": ...}},
     "cwd": "...", "transcript_path": "...", "session_id": "...", ...}

It stores it nowhere, so the only way to read it is to be that command.
``burnometer statusline`` is: it keeps the two percentages and their reset
times, writes them to one small file the scanner reads, and prints a short
status line (or, with ``--then``, whatever the user's own status line printed).

What this is not:

* **Not a network call, and not a credential.** Claude Code runs this command
  itself and passes what it already knows. Nothing here authenticates or asks
  Anthropic for anything.
* **Not a copy of the payload.** The same stdin carries the working directory,
  the transcript path and the session id. Only the four allowlisted numbers are
  extracted; the payload is never written, logged or echoed.

It must never break the status line it sits in. Every failure is swallowed and
the command always exits 0: a meter that takes down the thing it measures is
worse than no meter.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import burn_home
from .safety import harden_path, pluck_float, pluck_mapping, secure_dir

__all__ = [
    "CAPTURE_NAME",
    "WINDOWS",
    "capture_path",
    "claude_settings_path",
    "extract",
    "install",
    "is_installed",
    "merge",
    "record",
    "render",
    "run",
    "uninstall",
]

#: The file the scanner reads, in burn-o-meter's own ``0700`` directory.
CAPTURE_NAME = "claude-rate-limits.json"

#: Claude Code's window names are already ours, so they map straight across.
WINDOWS = ("five_hour", "seven_day")

#: A status line payload is a few kilobytes. Anything past this is not one, and
#: is not parsed at all rather than parsed partially.
MAX_INPUT_BYTES = 1 << 20

#: How long a wrapped status line command may take. Claude Code cancels a slow
#: status line itself; this only stops an orphan from outliving it.
THEN_TIMEOUT_SECONDS = 5


def capture_path() -> Path:
    return burn_home() / CAPTURE_NAME


def _reset_iso(value: Any) -> str | None:
    """A reset time as ISO 8601 UTC, from epoch seconds or an ISO string.

    The header behind this field carries epoch seconds; the service's usage
    endpoint uses ISO strings. Both are accepted, and anything else is dropped
    rather than guessed at - a wrong reset time is worse than none.
    """
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            seconds = float(value)
            # Milliseconds would put the reset tens of thousands of years out.
            if seconds > 1e11:
                seconds /= 1000
            when = datetime.fromtimestamp(seconds, tz=UTC)
        elif isinstance(value, str) and value:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if when.tzinfo is None:
                return None
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    return when.astimezone(UTC).isoformat(timespec="seconds")


def extract(payload: Any) -> dict[str, dict[str, Any]]:
    """The rate-limit windows in ``payload``, and nothing else from it.

    Returns ``{window: {"used_percent": float, "resets_at": str | None}}`` for
    each window present with a usable percentage. Empty when there are none,
    which is normal: API-key and third-party-provider sessions have no plan
    limits, and Claude Code sends ``rate_limits`` only once it has a figure.
    """
    if not isinstance(payload, Mapping):
        return {}
    limits = pluck_mapping(payload, "rate_limits")
    out: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        entry = pluck_mapping(limits, window)
        percent = pluck_float(entry, "used_percentage")
        if percent is None or not 0 <= percent <= 100:
            # Outside the documented range is not a reading we can stand behind.
            continue
        out[window] = {
            "used_percent": round(percent, 1),
            "resets_at": _reset_iso(entry.get("resets_at") if entry else None),
        }
    return out


def merge(
    previous: Mapping[str, Any], fresh: Mapping[str, Mapping[str, Any]], observed_at: str
) -> dict[str, Any]:
    """Combine a new capture with the one already on disk.

    Several Claude Code sessions can be open at once, and each reports the figure
    from its *own* last response. An idle session redrawing its status line would
    otherwise overwrite a busy session's 60% with its own stale 20%. Two rules
    stop that, both properties of the window rather than guesses:

    * **Within one window, usage only rises.** Same reset time, keep the higher.
    * **A later reset time is a later window.** Never replace it with an earlier.

    A window with no reset time cannot be ordered, so the fresh reading wins.
    """
    windows = dict(previous.get("windows") or {}) if isinstance(previous, Mapping) else {}
    for name, reading in fresh.items():
        old = windows.get(name)
        keep_old = False
        if isinstance(old, Mapping) and old.get("resets_at") and reading.get("resets_at"):
            if old["resets_at"] > reading["resets_at"]:
                keep_old = True
            elif old["resets_at"] == reading["resets_at"]:
                keep_old = (old.get("used_percent") or 0) > reading["used_percent"]
        if keep_old:
            # Re-confirmed, not re-observed: its timestamp stays its own.
            continue
        windows[name] = {**reading, "observed_at": observed_at}
    return {"version": 1, "windows": windows}


def _load(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(MAX_INPUT_BYTES))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(fresh: Mapping[str, Mapping[str, Any]], *, now: datetime | None = None) -> Path:
    """Merge ``fresh`` into the capture file, atomically and privately."""
    path = capture_path()
    secure_dir(path.parent)
    observed = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds")
    document = merge(_load(path), fresh, observed)

    # Written beside the target and renamed over it, so the scanner never reads
    # half a file. mkstemp creates it 0600 and refuses an existing name.
    fd, tmp = tempfile.mkstemp(prefix=".rate-limits-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(document, sort_keys=True).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    harden_path(path)
    return path


def render(windows: Mapping[str, Mapping[str, Any]]) -> str:
    """The status line printed when no other command is wrapped."""
    labels = {"five_hour": "5h", "seven_day": "7d"}
    parts = [
        f"{labels[name]} {round(windows[name]['used_percent'])}%"
        for name in WINDOWS
        if name in windows
    ]
    return " · ".join(parts)


def run(raw: bytes, then: str | None = None) -> str:
    """Capture what ``raw`` carries and return the text to print.

    Never raises: a failure to parse or record leaves the status line exactly as
    it would have been without burn-o-meter in front of it.
    """
    windows: dict[str, dict[str, Any]] = {}
    if len(raw) <= MAX_INPUT_BYTES:
        try:
            windows = extract(json.loads(raw))
        except (ValueError, UnicodeDecodeError, RecursionError):
            windows = {}
        if windows:
            # The status line must survive a capture that could not be written.
            with contextlib.suppress(Exception):
                record(windows)

    if then:
        try:
            # The user's own status line command, taken from their own Claude Code
            # settings and run the way Claude Code runs it: through the shell,
            # with the same stdin.
            done = subprocess.run(  # noqa: S602
                then,
                shell=True,
                input=raw,
                capture_output=True,
                timeout=THEN_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.decode("utf-8", errors="replace").rstrip("\n")
    return render(windows)


# --------------------------------------------------------------------------
# Installing the hook into Claude Code's settings
# --------------------------------------------------------------------------

#: Executable names this project installs under.
_ENGINE_NAMES = frozenset({"burnometer", "burn-o-meter"})


def claude_settings_path() -> Path:
    """Claude Code's user settings, honouring ``CLAUDE_CONFIG_DIR``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude"
    return base / "settings.json"


def _tokens(command: Any) -> list[str]:
    if not isinstance(command, str):
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _is_ours(status_line: Any) -> bool:
    if not isinstance(status_line, Mapping):
        return False
    tokens = _tokens(status_line.get("command"))
    # `python -m burnometer statusline` as well as the plain executable.
    for i, token in enumerate(tokens[:3]):
        if Path(token).name in _ENGINE_NAMES:
            return tokens[i + 1 : i + 2] == ["statusline"]
    return False


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object; leaving it untouched")
    return data


def _write_settings(path: Path, settings: Mapping[str, Any]) -> None:
    """Replace the settings file atomically, keeping its permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install(engine: list[str]) -> str:
    """Point Claude Code's status line at this engine. Returns what happened.

    An existing status line is kept, not replaced: it is wrapped with ``--then``
    so it still prints exactly what it did, and ``uninstall`` restores it. Other
    settings are carried over untouched; only ``statusLine`` changes.
    """
    path = claude_settings_path()
    settings = _read_settings(path)
    current = settings.get("statusLine")
    command = shlex.join([*engine, "statusline"])
    if _is_ours(current):
        # Re-pointed rather than left alone: switching from a development copy
        # to Homebrew, say, moves the engine, and the old path would go on
        # working until the day it is deleted.
        tokens = _tokens(current.get("command"))
        wrapped = tokens[tokens.index("--then") + 1] if "--then" in tokens[:-1] else None
        wanted = f"{command} --then {shlex.quote(wrapped)}" if wrapped else command
        if current.get("command") == wanted:
            return "already installed"
        settings["statusLine"] = {**current, "command": wanted}
        _write_settings(path, settings)
        return "re-pointed at this engine"

    if current is None:
        settings["statusLine"] = {"type": "command", "command": command}
        outcome = "installed"
    else:
        existing = current.get("command") if isinstance(current, Mapping) else None
        if not (
            isinstance(current, Mapping)
            and current.get("type") == "command"
            and isinstance(existing, str)
            and existing.strip()
        ):
            raise ValueError("the existing statusLine is not a command; leaving it untouched")
        settings["statusLine"] = {
            **current,
            "command": f"{command} --then {shlex.quote(existing)}",
        }
        outcome = "installed in front of your existing status line"
    _write_settings(path, settings)
    return outcome


def uninstall() -> str:
    """Undo :func:`install`, restoring a status line it wrapped."""
    path = claude_settings_path()
    settings = _read_settings(path)
    current = settings.get("statusLine")
    if not _is_ours(current):
        return "not installed"

    tokens = _tokens(current.get("command"))
    if "--then" in tokens and tokens.index("--then") + 1 < len(tokens):
        original = tokens[tokens.index("--then") + 1]
        settings["statusLine"] = {**current, "command": original}
        outcome = "removed; your previous status line is restored"
    else:
        del settings["statusLine"]
        outcome = "removed"
    _write_settings(path, settings)
    return outcome


def is_installed() -> bool:
    try:
        return _is_ours(_read_settings(claude_settings_path()).get("statusLine"))
    except (OSError, ValueError):
        return False


def main(then: str | None = None) -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    text = run(raw, then)
    if text:
        sys.stdout.write(text + "\n")
    return 0
