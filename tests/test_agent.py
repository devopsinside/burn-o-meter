"""Background scanning: the launchd job description and its lifecycle.

These tests never touch the real ``~/Library/LaunchAgents`` and never invoke
``launchctl`` — the plist path is redirected and loading is disabled.
"""

from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from burnometer import agent


@pytest.fixture
def fake_agent_paths(tmp_path: Path, burn_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plist = tmp_path / "LaunchAgents" / f"{agent.AGENT_LABEL}.plist"
    monkeypatch.setattr(agent, "agent_plist_path", lambda: plist)
    calls: list[list[str]] = []
    monkeypatch.setattr(agent, "_run", lambda args: calls.append(args) or _Ok())
    return plist


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def test_plist_has_the_fields_launchd_needs(burn_home: Path) -> None:
    p = agent.build_plist(interval=60)
    assert p["Label"] == agent.AGENT_LABEL
    assert p["StartInterval"] == 60
    assert p["RunAtLoad"] is True
    assert p["ProgramArguments"][-2:] == ["scan", "--quiet"]


def test_agent_runs_at_low_priority(burn_home: Path) -> None:
    """It must never compete with the editor the user is actually working in."""
    p = agent.build_plist()
    assert p["ProcessType"] == "Background"
    assert p["LowPriorityIO"] is True
    assert p["Nice"] > 0


def test_too_frequent_an_interval_is_refused(burn_home: Path) -> None:
    """Scanning every second would burn power for no visible benefit."""
    with pytest.raises(ValueError, match="at least"):
        agent.build_plist(interval=1)


def test_install_writes_a_readable_plist(fake_agent_paths: Path, burn_home: Path) -> None:
    path = agent.install_agent(interval=45, load=False)
    assert path.exists()
    # launchd must read this, so 0644 rather than 0600. It holds no secrets.
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    with open(path, "rb") as fh:
        assert plistlib.load(fh)["StartInterval"] == 45


def test_agent_log_is_owner_only(fake_agent_paths: Path, burn_home: Path) -> None:
    """Adapter errors land here. They are redacted by construction, but the file
    is still kept private."""
    agent.install_agent(load=False)
    log = agent.agent_log_path()
    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_status_reflects_lifecycle(fake_agent_paths: Path, burn_home: Path) -> None:
    assert agent.agent_status().installed is False
    agent.install_agent(interval=30, load=False)
    status = agent.agent_status()
    assert status.installed is True
    assert status.interval == 30
    assert agent.uninstall_agent() is True
    assert agent.agent_status().installed is False


def test_uninstall_is_safe_when_nothing_is_installed(
    fake_agent_paths: Path, burn_home: Path
) -> None:
    assert agent.uninstall_agent() is False


def test_describe_never_leaks_a_home_path(fake_agent_paths: Path, burn_home: Path) -> None:
    agent.install_agent(load=False)
    for line in agent.describe(agent.agent_status()):
        assert str(Path.home()) not in line
