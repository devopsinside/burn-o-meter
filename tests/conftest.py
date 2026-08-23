"""Shared fixtures.

The network blocker is ``autouse``, so **every** test in this suite runs with
outbound sockets disabled. That turns G3 ("zero network by default") from a
promise into a property of the build: if any code path ever grows an implicit
HTTP call — a telemetry ping, an update check, a pricing fetch on import — the
suite fails rather than silently phoning home.

The single legitimate egress (``burnometer pricing refresh``) opts back in with
``@pytest.mark.allow_network``.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest


class NetworkAccessBlocked(RuntimeError):
    """Raised when a test attempts an outbound connection."""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: permit outbound sockets (only for the pricing refresh path)",
    )


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test on any outbound connection attempt."""
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(*args: object, **kwargs: object):
        raise NetworkAccessBlocked(
            "outbound network access attempted; burn-o-meter must not make network "
            "calls outside `pricing refresh` (guarantee G3)"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    if hasattr(socket, "socketpair"):
        pass  # local socketpairs are fine; they cannot leave the machine


@pytest.fixture
def burn_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.burn-o-meter`` into a temp dir for the duration of a test."""
    home = tmp_path / "burn-home"
    monkeypatch.setenv("BURNOMETER_HOME", str(home))
    return home


@pytest.fixture
def store(burn_home: Path):
    """An open, writable store in a temporary home."""
    from burnometer.store import Store

    with Store.open(burn_home / "burn.db") as s:
        yield s


@pytest.fixture
def fake_agent_tree(tmp_path: Path) -> Path:
    """A miniature ``~`` containing both provider trees *and* credential files.

    Adapters are pointed at this in tests so the credential trap is exercised on
    every run: if a glob is ever widened, ``auth.json`` is right there to be
    caught by it.
    """
    root = tmp_path / "home"
    (root / ".claude" / "projects" / "-Users-x-proj").mkdir(parents=True)
    (root / ".claude" / "sessions").mkdir(parents=True)
    (root / ".codex" / "sessions" / "2026" / "08" / "21").mkdir(parents=True)

    # Credential bait — must never be opened.
    (root / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY":"sk-CANARY-CREDENTIAL"}')
    (root / ".gemini").mkdir(parents=True)
    (root / ".gemini" / "oauth_creds.json").write_text('{"refresh_token":"CANARY-CREDENTIAL"}')
    (root / ".claude" / "sessions" / "1234.abcdef.key").write_text("CANARY-CREDENTIAL")

    # A symlink disguised as a log file, pointing at the credential store.
    disguised = root / ".claude" / "projects" / "-Users-x-proj" / "evil.jsonl"
    os.symlink(root / ".codex" / "auth.json", disguised)

    return root
