"""Background scanning via a macOS LaunchAgent.

Deliberately *not* a long-running daemon. launchd re-runs ``burn-o-meter scan`` on
an interval instead, which means no resident process, no memory growth, nothing
to leak a file handle, and automatic recovery if a run fails — launchd simply
tries again next interval. An incremental scan with nothing to do costs about
five milliseconds, so the interval is close to free.

The agent is a good citizen by construction: background process type, low
priority I/O and a positive nice value, so it never competes with the editor the
user is actually working in.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import burn_home
from .safety import harden_path, redact_path, secure_dir

__all__ = [
    "AGENT_LABEL",
    "AgentStatus",
    "agent_plist_path",
    "build_plist",
    "install_agent",
    "uninstall_agent",
    "agent_status",
    "DEFAULT_INTERVAL",
]

AGENT_LABEL = "com.burn-o-meter.scan"
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 15


@dataclass(slots=True)
class AgentStatus:
    installed: bool
    loaded: bool
    plist_path: Path
    interval: int | None = None
    program: str | None = None
    log_path: Path | None = None
    last_log_line: str | None = None


def agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def agent_log_path() -> Path:
    return burn_home() / "agent.log"


def _resolve_executable() -> str:
    """Find the burnometer entry point to schedule.

    Prefers the installed console script; falls back to ``python -m burnometer``
    so a venv or editable install still works.
    """
    found = shutil.which("burnometer")
    if found:
        return found
    candidate = Path(sys.executable).parent / "burnometer"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def build_plist(*, interval: int = DEFAULT_INTERVAL) -> dict:
    """Build the launchd job description."""
    if interval < MIN_INTERVAL:
        raise ValueError(
            f"interval must be at least {MIN_INTERVAL}s; "
            f"scanning more often than that wastes power for no visible benefit"
        )

    executable = _resolve_executable()
    args = [executable, "scan", "--quiet"]
    if Path(executable).name.startswith("python"):
        args = [executable, "-m", "burnometer", "scan", "--quiet"]

    log = agent_log_path()
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": args,
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        # Never compete with the user's editor for CPU or disk.
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        # A scan is incremental and quick; if one wedges, do not let it linger.
        "ExitTimeOut": 120,
    }


def install_agent(*, interval: int = DEFAULT_INTERVAL, load: bool = True) -> Path:
    """Write the plist and, unless told otherwise, load it into launchd."""
    plist = build_plist(interval=interval)
    path = agent_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    secure_dir(burn_home())
    log = agent_log_path()
    if not log.exists():
        log.touch()
    # The log can carry adapter errors. Those are redacted by construction, but
    # keep it owner-only regardless.
    harden_path(log)

    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    # launchd must be able to read this, so 0644 rather than 0600. It contains
    # no secrets — only a path and an interval.
    path.chmod(0o644)

    if load:
        _launchctl_reload(path)
    return path


def uninstall_agent() -> bool:
    """Unload and remove the agent. Returns whether anything was there."""
    path = agent_plist_path()
    existed = path.exists()
    _launchctl_bootout()
    if existed:
        path.unlink()
    return existed


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _launchctl_bootout() -> None:
    _run(["launchctl", "bootout", f"{_domain()}/{AGENT_LABEL}"])


def _launchctl_reload(path: Path) -> None:
    # bootout first so a re-install picks up a changed interval; a missing job
    # makes this a no-op, which is why its exit status is ignored.
    _launchctl_bootout()
    _run(["launchctl", "bootstrap", _domain(), str(path)])


def agent_status() -> AgentStatus:
    path = agent_plist_path()
    if not path.exists():
        return AgentStatus(installed=False, loaded=False, plist_path=path)

    try:
        with open(path, "rb") as fh:
            plist = plistlib.load(fh)
    except Exception:
        return AgentStatus(installed=True, loaded=False, plist_path=path)

    listed = _run(["launchctl", "list", AGENT_LABEL])
    log = agent_log_path()
    last_line = None
    if log.exists() and log.stat().st_size:
        try:
            lines = [ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
            last_line = lines[-1] if lines else None
        except OSError:
            last_line = None

    return AgentStatus(
        installed=True,
        loaded=listed.returncode == 0,
        plist_path=path,
        interval=plist.get("StartInterval"),
        program=" ".join(plist.get("ProgramArguments", [])),
        log_path=log,
        last_log_line=last_line,
    )


def describe(status: AgentStatus) -> list[str]:
    """Human-readable status lines, safe to print."""
    out = [f"plist    {redact_path(status.plist_path)}"]
    if not status.installed:
        out.append("state    not installed")
        return out
    out.append(f"state    {'loaded' if status.loaded else 'installed but not loaded'}")
    if status.interval:
        out.append(f"interval every {status.interval}s")
    if status.program:
        # The program path runs through a venv under the user's home, so it must
        # be redacted like any other path before it can reach a bug report.
        parts = [redact_path(part) if "/" in part else part for part in status.program.split()]
        out.append(f"runs     {' '.join(parts)}")
    if status.log_path:
        out.append(f"log      {redact_path(status.log_path)}")
    if status.last_log_line:
        out.append(f"last     {status.last_log_line}")
    return out
