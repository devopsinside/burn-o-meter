"""Claude Code's live rate limits, captured through its status line.

The payload Claude Code sends carries the working directory, transcript path and
session id beside the rate limits, and the hook runs inside a UI it must never
break. Those two facts are what most of these tests are about.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from burnometer import statusline
from burnometer.adapters.claude_statusline import ClaudeStatuslineAdapter
from burnometer.models import QuotaSource

CANARY = "CANARY-statusline-7f3a"
RESET = 1_790_000_000  # epoch seconds, as the reset header carries it


def _payload(five: float | None = 34.5, week: float | None = 15.0, **extra) -> dict:
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": RESET}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": RESET + 86_400}
    return {
        "session_id": f"{CANARY}-session",
        "transcript_path": f"/Users/someone/.claude/projects/{CANARY}/t.jsonl",
        "cwd": f"/Users/someone/{CANARY}-client-project",
        "model": {"id": "claude-opus-5-5", "display_name": f"{CANARY} model"},
        "workspace": {"current_dir": f"/Users/someone/{CANARY}"},
        "cost": {"total_cost_usd": 1.23},
        "rate_limits": limits,
        **extra,
    }


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def _capture(burn_home: Path) -> dict:
    return json.loads((burn_home / statusline.CAPTURE_NAME).read_text())


# -- what is kept -------------------------------------------------------------


def test_statusline_keeps_only_the_rate_limits(burn_home: Path) -> None:
    """G1: four numbers survive; the directory, transcript path, session id and
    model name sent beside them are never written or echoed."""
    out = statusline.run(json.dumps(_payload()).encode())

    blob = (burn_home / statusline.CAPTURE_NAME).read_bytes()
    assert CANARY.encode() not in blob
    assert b"/Users/" not in blob
    assert CANARY not in out

    document = json.loads(blob)
    assert set(document) == {"version", "windows"}
    assert set(document["windows"]) == {"five_hour", "seven_day"}
    for reading in document["windows"].values():
        assert set(reading) == {"used_percent", "resets_at", "observed_at"}
    assert document["windows"]["five_hour"]["used_percent"] == 34.5


def test_statusline_capture_is_private(burn_home: Path) -> None:
    """G4: owner-only, in an owner-only directory, with no temp file left over."""
    statusline.run(json.dumps(_payload()).encode())
    path = burn_home / statusline.CAPTURE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(burn_home.stat().st_mode) == 0o700
    assert [p.name for p in burn_home.iterdir()] == [statusline.CAPTURE_NAME]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[1, 2]",
        b"null",
        b"[" * 100_000,  # deep enough to exhaust the parser's recursion
        # Valid, and carrying limits: refused for its size alone.
        (json.dumps(_payload()) + " " * statusline.MAX_INPUT_BYTES).encode(),
        json.dumps({"rate_limits": "high"}).encode(),
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": "34"}}}).encode(),
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": True}}}).encode(),
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 340}}}).encode(),
        b'{"rate_limits": {"five_hour": {"used_percentage": NaN}}}',
        b"\xff\xfe\x00garbage",
    ],
)
def test_statusline_never_fails(burn_home: Path, raw: bytes) -> None:
    """The hook sits inside Claude Code's UI. Bad input records nothing, prints
    nothing, and does not raise."""
    assert statusline.run(raw) == ""
    assert not (burn_home / statusline.CAPTURE_NAME).exists()


def test_statusline_survives_an_unwritable_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BURNOMETER_HOME", str(blocker / "home"))
    assert statusline.run(json.dumps(_payload()).encode()) == "5h 34% · 7d 15%"


def test_the_command_exits_zero_and_prints_the_status_line(
    burn_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from burnometer.cli import main

    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(_payload(five=89.6)).encode()))
    monkeypatch.setattr(sys, "stdin", stdin)
    assert main(["statusline"]) == 0
    # Rounded as the menu bar rounds it, so the two never disagree.
    assert capsys.readouterr().out == "5h 90% · 7d 15%\n"


def test_no_limits_prints_nothing(burn_home: Path) -> None:
    """API-key and third-party sessions have no plan limits; Claude Code omits
    the field. An empty status line, not a '0%' that would be a claim."""
    assert statusline.run(json.dumps(_payload(five=None, week=None)).encode()) == ""
    assert not (burn_home / statusline.CAPTURE_NAME).exists()


# -- reset times --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (RESET, datetime.fromtimestamp(RESET, UTC)),
        (RESET * 1000, datetime.fromtimestamp(RESET, UTC)),
        ("2026-09-28T16:30:00Z", datetime(2026, 9, 28, 16, 30, tzinfo=UTC)),
        ("2026-09-28T22:00:00+05:30", datetime(2026, 9, 28, 16, 30, tzinfo=UTC)),
        ("2026-09-28T16:30:00", None),  # no zone: which 16:30?
        ("soon", None),
        (True, None),
        (None, None),
        (10**30, None),
    ],
)
def test_reset_times_are_read_or_refused(value, expected) -> None:
    got = statusline._reset_iso(value)
    assert (datetime.fromisoformat(got) if got else None) == expected


# -- several sessions at once -------------------------------------------------


def _reading(percent: float, resets: str | None) -> dict:
    return {"used_percent": percent, "resets_at": resets}


def test_a_stale_session_cannot_lower_the_figure() -> None:
    """Within one window usage only rises. An idle session redrawing its status
    line reports its own last response, which can be far behind a busy one."""
    now = "2026-09-28T12:00:00.000+00:00"
    busy = statusline.merge({}, {"five_hour": _reading(60, "T2")}, "2026-09-28T11:59")
    merged = statusline.merge(busy, {"five_hour": _reading(20, "T2")}, now)
    assert merged["windows"]["five_hour"]["used_percent"] == 60
    assert merged["windows"]["five_hour"]["observed_at"] == "2026-09-28T11:59"


def test_a_later_window_replaces_an_earlier_one_but_not_the_reverse() -> None:
    old_window = statusline.merge({}, {"five_hour": _reading(95, "T1")}, "a")
    new_window = statusline.merge(old_window, {"five_hour": _reading(4, "T2")}, "b")
    assert new_window["windows"]["five_hour"]["used_percent"] == 4
    stale = statusline.merge(new_window, {"five_hour": _reading(95, "T1")}, "c")
    assert stale["windows"]["five_hour"]["used_percent"] == 4


def test_a_reading_without_a_reset_time_simply_wins() -> None:
    first = statusline.merge({}, {"five_hour": _reading(50, None)}, "a")
    second = statusline.merge(first, {"five_hour": _reading(10, None)}, "b")
    assert second["windows"]["five_hour"]["used_percent"] == 10


# -- --then -------------------------------------------------------------------


def test_then_runs_the_wrapped_command_with_the_same_input(burn_home: Path) -> None:
    """An existing status line keeps printing exactly what it did."""
    raw = json.dumps(_payload()).encode()
    echo = f"{sys.executable} -c \"import sys; print('mine:', len(sys.stdin.buffer.read()))\""
    assert statusline.run(raw, then=echo) == f"mine: {len(raw)}"
    # ...and the capture still happened.
    assert _capture(burn_home)["windows"]["five_hour"]["used_percent"] == 34.5


def test_a_failing_wrapped_command_does_not_fail_the_hook(burn_home: Path) -> None:
    assert statusline.run(json.dumps(_payload()).encode(), then="exit 3") == ""


# -- installing ---------------------------------------------------------------

ENGINE = [sys.executable, "-m", "burnometer"]


def test_install_changes_only_the_status_line(claude_dir: Path, burn_home: Path) -> None:
    """Every other key survives, including an API key in `env`, and no copy of
    the file is made anywhere - it would be a second place that key lives."""
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    before = {
        "env": {"ANTHROPIC_API_KEY": f"sk-{CANARY}"},
        "permissions": {"allow": ["Bash(ls:*)"]},
        "model": "opus",
    }
    settings.write_text(json.dumps(before))
    settings.chmod(0o640)

    assert statusline.install(ENGINE) == "installed"

    after = json.loads(settings.read_text())
    assert {k: v for k, v in after.items() if k != "statusLine"} == before
    assert after["statusLine"]["type"] == "command"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert [p.name for p in claude_dir.iterdir()] == ["settings.json"]
    assert not burn_home.exists() or all(
        CANARY.encode() not in p.read_bytes() for p in burn_home.rglob("*") if p.is_file()
    )


def test_install_is_idempotent_and_uninstall_removes_it(claude_dir: Path) -> None:
    assert statusline.install(ENGINE) == "installed"
    assert statusline.is_installed()
    assert statusline.install(ENGINE) == "already installed"
    assert statusline.uninstall() == "removed"
    assert "statusLine" not in json.loads((claude_dir / "settings.json").read_text())
    assert statusline.uninstall() == "not installed"


def test_an_existing_status_line_is_wrapped_and_restored_exactly(
    claude_dir: Path, burn_home: Path
) -> None:
    """Quotes, a pipe and a variable in the user's own command survive the round
    trip through --then, and the wrapped command really runs."""
    claude_dir.mkdir()
    original = {
        "type": "command",
        "command": "printf '%s' \"it's $HOME\" | tr a-z A-Z",
        "padding": 0,
    }
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": original}))

    assert "existing" in statusline.install(ENGINE)
    installed = json.loads((claude_dir / "settings.json").read_text())["statusLine"]
    assert installed["padding"] == 0

    done = subprocess.run(
        installed["command"],
        shell=True,
        input=json.dumps(_payload()).encode(),
        capture_output=True,
        check=True,
        env={"HOME": "/home/x", "PATH": os.environ["PATH"], "BURNOMETER_HOME": str(burn_home)},
    )
    assert done.stdout.decode().strip() == "IT'S /HOME/X"
    assert _capture(burn_home)["windows"]["five_hour"]["used_percent"] == 34.5

    assert "restored" in statusline.uninstall()
    assert json.loads((claude_dir / "settings.json").read_text())["statusLine"] == original


def test_reinstalling_from_another_engine_repoints_it(claude_dir: Path) -> None:
    """Moving from a development copy to Homebrew must not leave the status line
    pointing at the old one - and must keep the command it wraps."""
    claude_dir.mkdir()
    original = {"type": "command", "command": "echo mine"}
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": original}))
    statusline.install(["/old/venv/bin/burnometer"])

    assert statusline.install(["/opt/homebrew/bin/burnometer"]) == "re-pointed at this engine"
    command = json.loads((claude_dir / "settings.json").read_text())["statusLine"]["command"]
    assert command.startswith("/opt/homebrew/bin/burnometer statusline --then ")
    assert "/old/venv" not in command
    assert statusline.install(["/opt/homebrew/bin/burnometer"]) == "already installed"
    statusline.uninstall()
    assert json.loads((claude_dir / "settings.json").read_text())["statusLine"] == original


@pytest.mark.parametrize(
    "content",
    ["[1, 2]", "{not json", json.dumps({"statusLine": {"type": "static", "text": "hi"}})],
)
def test_install_refuses_what_it_does_not_understand(claude_dir: Path, content: str) -> None:
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(content)
    with pytest.raises(ValueError):
        statusline.install(ENGINE)
    assert settings.read_text() == content


# -- reading it back ----------------------------------------------------------


def test_the_adapter_reports_exact_readings_with_the_services_reset(burn_home: Path) -> None:
    statusline.run(json.dumps(_payload()).encode())
    path = burn_home / statusline.CAPTURE_NAME
    result = ClaudeStatuslineAdapter().parse(path, burn_home)

    by_window = {q.window_name: q for q in result.quotas}
    assert set(by_window) == {"five_hour", "seven_day"}
    five = by_window["five_hour"]
    assert five.provider == "claude"
    assert five.source is QuotaSource.EXACT
    assert five.used_percent == 34.5
    assert five.window_minutes == 300
    assert five.resets_at == datetime.fromtimestamp(RESET, UTC)


def test_the_newer_live_reading_wins_over_the_desktop_sample(burn_home: Path) -> None:
    """The failure that prompted this source: the desktop app's last sample said
    0% an hour into a window Claude Code was using. Scanned together, the
    menu bar gets the live figure."""
    from burnometer.models import QuotaSnapshot
    from burnometer.scan import scan
    from burnometer.snapshot import build_snapshot
    from burnometer.store import Store

    reset = datetime.now(UTC) + timedelta(hours=4)
    statusline.run(
        json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": 41, "resets_at": reset.timestamp()}}}
        ).encode()
    )
    with Store.open(burn_home / "burn.db") as store:
        store.record_quota(
            [
                QuotaSnapshot(
                    provider="claude",
                    window_name="five_hour",
                    used_percent=0.0,
                    observed_at=datetime.now(UTC) - timedelta(minutes=50),
                    source=QuotaSource.EXACT,
                    window_minutes=300,
                )
            ]
        )
        # Only this adapter: a bare scan() would read the real ~/.claude.
        scan(store, adapters=[ClaudeStatuslineAdapter()])
        quotas = build_snapshot(store)["quotas"]

    five = [q for q in quotas if q["provider"] == "claude" and q["window"] == "five_hour"]
    assert len(five) == 1
    assert five[0]["used_percent"] == 41
    assert five[0]["exact"] is True
