"""The security guarantees, asserted rather than documented.

Every test here corresponds to a numbered guarantee in ``SECURITY.md``. If one
of these fails, a guarantee has regressed and the build must not ship.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
from pathlib import Path

import pytest

from burnometer import safety
from burnometer.safety import (
    CredentialAccessBlocked,
    PathEscapeBlocked,
    UnsafeFileType,
    assert_within,
    is_credential_path,
    open_log_readonly,
    project_label,
    redact,
    redact_path,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "burnometer"


# ---------------------------------------------------------------- G2 --------


@pytest.mark.parametrize(
    "path",
    [
        "~/.codex/auth.json",
        "~/.gemini/oauth_creds.json",
        "~/.claude/sessions/32373.abc.key",
        "~/.ssh/id_rsa",
        "~/.aws/credentials",
        "/x/.codex/.codex-global-state.json",
        "/x/secrets.pem",
        "/x/.env",
        "/x/AUTH.JSON",
    ],
)
def test_credential_paths_are_recognised(path: str) -> None:
    assert is_credential_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "~/.claude/projects/p/session.jsonl",
        "~/.codex/sessions/2026/08/21/rollout-x.jsonl",
    ],
)
def test_ordinary_logs_are_not_flagged(path: str) -> None:
    assert is_credential_path(path) is False


def test_credential_file_is_never_opened(fake_agent_tree: Path) -> None:
    """G2: even asked directly, the deny-list refuses."""
    auth = fake_agent_tree / ".codex" / "auth.json"
    assert auth.exists(), "fixture should have planted the bait"
    with pytest.raises(CredentialAccessBlocked):
        open_log_readonly(fake_agent_tree, auth)


def test_symlink_to_credentials_is_blocked(fake_agent_tree: Path) -> None:
    """G2: a file named *.jsonl that is really a link to auth.json.

    This is the attack the deny-list alone would miss, since the name looks
    entirely legitimate. Containment catches it because the link *resolves*
    outside the projects tree.
    """
    disguised = fake_agent_tree / ".claude" / "projects" / "-Users-x-proj" / "evil.jsonl"
    assert disguised.is_symlink()
    root = fake_agent_tree / ".claude" / "projects"
    with pytest.raises((PathEscapeBlocked, CredentialAccessBlocked)):
        open_log_readonly(root, disguised)


def test_symlink_within_root_is_still_refused(tmp_path: Path) -> None:
    """Even a link that stays inside the root is refused.

    lstat rejects it without following, and O_NOFOLLOW would catch it again if
    the entry were swapped between the check and the open. A log file has no
    legitimate reason to be a symlink, so refusing is cheaper than reasoning
    about where each one points.
    """
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real.jsonl"
    real.write_bytes(b'{"ok":1}\n')
    link = root / "link.jsonl"
    os.symlink(real, link)
    with pytest.raises((UnsafeFileType, OSError)):
        open_log_readonly(root, link)


def test_path_escape_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}")
    with pytest.raises(PathEscapeBlocked):
        assert_within(root, outside)


def test_assert_within_allows_legitimate_descendant(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    f = root / "a" / "b" / "c.jsonl"
    f.write_text("{}")
    assert assert_within(root, f) == f.resolve()


def test_non_regular_file_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "pipe.jsonl"
    os.mkfifo(fifo)
    with pytest.raises((UnsafeFileType, OSError)):
        open_log_readonly(root, fifo)


def test_ordinary_log_opens_fine(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    f = root / "ok.jsonl"
    f.write_bytes(b'{"type":"assistant"}\n')
    with open_log_readonly(root, f) as fh:
        assert fh.read() == b'{"type":"assistant"}\n'


# ---------------------------------------------------------------- G3 --------


def test_network_is_blocked_by_default() -> None:
    """G3: the autouse fixture must actually bite."""
    from conftest import NetworkAccessBlocked

    with pytest.raises(NetworkAccessBlocked):
        socket.create_connection(("example.com", 443), timeout=1)


def test_no_telemetry_or_http_clients_imported() -> None:
    """G3: no networking library may appear anywhere in the package.

    `pricing/catalog.py` will import urllib for the single opt-in refresh; it is
    allowlisted here by name so that adding networking anywhere *else* fails.
    """
    allowed = {"pricing/catalog.py"}
    offenders: list[str] = []
    pattern = re.compile(r"^\s*(?:import|from)\s+(requests|httpx|urllib|http\.client|aiohttp)\b")
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix()
        if rel in allowed:
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pattern.match(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "networking imported outside the allowlisted refresh path:\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------- G4 --------


def test_secure_dir_and_file_modes(tmp_path: Path) -> None:
    d = safety.secure_dir(tmp_path / "home")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700

    f = d / "x.db"
    with safety.secure_open_write(f) as fh:
        fh.write(b"x")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_secure_dir_tightens_existing_loose_permissions(tmp_path: Path) -> None:
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    safety.secure_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_all_sql_is_parameterised() -> None:
    """G4: no interpolation into SQL anywhere.

    Lines carrying an explicit ``sql-audited`` marker are exempt; there is
    currently exactly one (a PRAGMA, which SQLite cannot bind a parameter to).
    """
    sql_kw = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|PRAGMA)\b", re.I)
    interp = re.compile(r"""(f["']|\.format\(|%\s*\(|%\s*[sd]["'])""")
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "sql-audited" in line:
                continue
            if sql_kw.search(line) and interp.search(line):
                offenders.append(f"{py.relative_to(SRC).as_posix()}:{i}: {line.strip()}")
    assert not offenders, "interpolated SQL found:\n" + "\n".join(offenders)


# ---------------------------------------------------------------- G5 --------


def test_project_label_redaction_modes() -> None:
    cwd = "/Users/alice/work/acme-secret-merger"
    assert project_label(cwd, "full") == cwd
    assert project_label(cwd, "basename") == "acme-secret-merger"
    assert project_label(cwd, "none") is None

    hashed = project_label(cwd, "hash")
    assert hashed is not None and hashed.startswith("proj_")
    assert "alice" not in hashed and "acme" not in hashed
    assert hashed == project_label(cwd, "hash"), "must be stable for grouping"


def test_default_mode_drops_the_username() -> None:
    """The default must never record who the user is or where they work."""
    label = project_label("/Users/alice/clients/bigcorp/repo", "basename")
    assert label == "repo"
    assert "alice" not in (label or "")
    assert "bigcorp" not in (label or "")


def test_unknown_redaction_mode_rejected() -> None:
    with pytest.raises(ValueError):
        project_label("/x/y", "sneaky")


# ---------------------------------------------------------------- G6 --------


def test_redact_never_discloses_content() -> None:
    secret = "CANARY-SECRET-VALUE-abcdef123456"
    out = redact(secret)
    assert secret not in out
    assert "redacted" in out and str(len(secret)) in out

    assert "KEY" not in redact({"authorization": secret})
    assert "KEY" not in redact([secret, secret])


def test_redact_path_hides_home(tmp_path: Path) -> None:
    p = Path.home() / "work" / "secret-project"
    out = redact_path(p)
    assert out.startswith("~/")
    assert str(Path.home()) not in out


def test_adapter_error_carries_location_not_content() -> None:
    err = safety.AdapterError("/x/a.jsonl", 42, "invalid json")
    text = str(err)
    assert "a.jsonl" in text and "42" in text
    assert "invalid json" in text


# ---------------------------------------------------------------- G1 --------


def test_pluck_helpers_cannot_return_content() -> None:
    """G1: the extraction primitives return scalars or nothing, ever."""
    hostile = {
        "content": [{"type": "text", "text": "CANARY-PROMPT-TEXT"}],
        "model": {"nested": "CANARY-PROMPT-TEXT"},
        "input_tokens": {"evil": 1},
        "used_percent": "not-a-number",
        "ok_str": "claude-opus-5",
        "ok_int": 42,
    }
    assert safety.pluck_str(hostile, "model") is None, "dict must not pass as a string"
    assert safety.pluck_int(hostile, "input_tokens") == 0, "dict must not pass as an int"
    assert safety.pluck_float(hostile, "used_percent") is None
    assert safety.pluck_str(hostile, "ok_str") == "claude-opus-5"
    assert safety.pluck_int(hostile, "ok_int") == 42
    assert safety.pluck_mapping(hostile, "ok_str") is None


def test_pluck_str_caps_length() -> None:
    """A field far longer than any legitimate value is truncated, not carried."""
    huge = {"cwd": "A" * 10_000}
    out = safety.pluck_str(huge, "cwd")
    assert out is not None and len(out) == 512


def test_content_keys_are_not_in_the_metadata_allowlist() -> None:
    """G1: the two sets must never overlap, or content becomes extractable."""
    overlap = safety.CONTENT_KEYS & (safety.METADATA_FIELDS | safety.USAGE_FIELDS)
    assert not overlap, f"content-bearing keys leaked into an allowlist: {overlap}"


# ------------------------------------------------- G1: end-to-end canary ----
#
# The fixtures deliberately contain content-bearing fields whose values all
# start with "CANARY-": prompt text, tool results with a fake API key, session
# slugs. If any of them can be found in the database, the JSON output or a log
# line, the content firewall has failed and the build must not ship.

CANARY = b"CANARY-"

#: A credential-shaped value that is deliberately NOT key-shaped.
#: An earlier revision used a realistic "sk-ant-…" string. That trips GitHub
#: secret scanning and makes a reviewer stop to check whether a real key
#: leaked — a fake key that looks real is its own small hazard.
CREDENTIAL_CANARY = b"CANARY-CREDENTIAL-SHAPED-VALUE"
FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"


def _scan_fixtures_into(db_path: Path):
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.claude_code import ClaudeCodeAdapter
    from burnometer.scan import scan
    from burnometer.store import Store

    class Scoped(ClaudeCodeAdapter):
        def sources(self):
            return [LogSource(root=FIXTURES, glob="*.jsonl")]

    with Store.open(db_path) as store:
        report = scan(store, adapters=[Scoped()])
        assert store.count_events() > 0, "fixtures must actually produce events"
    return report


def test_fixtures_really_contain_canaries() -> None:
    """Guard the guard: if the fixtures lose their canaries, the scan below
    would pass vacuously and prove nothing."""
    for name in ("basic.jsonl", "edge.jsonl"):
        assert CANARY in (FIXTURES / name).read_bytes(), f"{name} lost its canaries"


def test_no_prompt_content_reaches_the_database(burn_home: Path) -> None:
    """G1, end to end: parse transcripts full of content, then read every byte
    of the resulting database back and find none of it."""
    db = burn_home / "burn.db"
    _scan_fixtures_into(db)

    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db) + suffix)
        if not f.exists():
            continue
        blob = f.read_bytes()
        assert CANARY not in blob, f"prompt content leaked into {f.name}"
        assert CREDENTIAL_CANARY not in blob, f"credential-shaped value leaked into {f.name}"


def test_no_content_reaches_stdout_during_a_scan(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """G6: nothing printed during a scan may quote a transcript."""
    from burnometer.cli import main

    assert main(["scan", "--stats"]) == 0
    captured = capsys.readouterr()
    assert "CANARY" not in captured.out + captured.err


def test_scan_state_stores_no_filesystem_paths(burn_home: Path) -> None:
    """G5: Claude Code names its project directories after the full working
    directory, so storing paths here would reintroduce exactly what
    project_paths strips."""
    import sqlite3

    db = burn_home / "burn.db"
    _scan_fixtures_into(db)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT path_key, path_label FROM scan_state").fetchall()
    conn.close()

    assert rows, "scan should have recorded progress"
    for key, label in rows:
        assert "/" not in key, "path_key must be a hash, not a path"
        assert len(key) == 32
        assert "/" not in (label or ""), "path_label must be a bare filename"
        assert str(FIXTURES) not in (label or "")


def test_stored_project_never_contains_a_home_path(burn_home: Path) -> None:
    """The default privacy mode must leave no absolute path in the store."""
    import sqlite3

    db = burn_home / "burn.db"
    _scan_fixtures_into(db)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    projects = [r[0] for r in conn.execute("SELECT DISTINCT project FROM usage_events")]
    conn.close()

    for p in projects:
        assert p is None or not p.startswith("/"), f"absolute path stored: {p}"
        assert "testuser" not in (p or ""), "account name leaked into the store"
        assert "bigcorp" not in (p or ""), "client name leaked into the store"


CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def test_codex_fixtures_really_contain_canaries() -> None:
    for name in ("session.jsonl", "reset.jsonl", "edge.jsonl"):
        assert CANARY in (CODEX_FIXTURES / name).read_bytes(), f"{name} lost its canaries"


def test_codex_system_prompts_never_reach_the_database(burn_home: Path) -> None:
    """Codex embeds its entire system prompt in ``session_meta.base_instructions``
    and agent replies in ``response_item`` payloads. Neither may be stored."""
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.codex import CodexAdapter
    from burnometer.scan import scan
    from burnometer.store import Store

    class Scoped(CodexAdapter):
        def sources(self):
            return [LogSource(root=CODEX_FIXTURES, glob="*.jsonl")]

    db = burn_home / "burn.db"
    with Store.open(db) as store:
        scan(store, adapters=[Scoped()])
        assert store.count_events() > 0

    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db) + suffix)
        if f.exists():
            blob = f.read_bytes()
            assert CANARY not in blob, f"Codex content leaked into {f.name}"
            assert CREDENTIAL_CANARY not in blob, f"credential-shaped value in {f.name}"


def test_both_providers_scan_clean_together(burn_home: Path) -> None:
    """The real configuration: two adapters, one store, no leakage."""
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.claude_code import ClaudeCodeAdapter
    from burnometer.adapters.codex import CodexAdapter
    from burnometer.scan import scan
    from burnometer.store import Store

    class ScopedClaude(ClaudeCodeAdapter):
        def sources(self):
            return [LogSource(root=FIXTURES, glob="*.jsonl")]

    class ScopedCodex(CodexAdapter):
        def sources(self):
            return [LogSource(root=CODEX_FIXTURES, glob="*.jsonl")]

    db = burn_home / "burn.db"
    with Store.open(db) as store:
        report = scan(store, adapters=[ScopedClaude(), ScopedCodex()])
        assert report.errors == []
        providers = set(store.stats()["providers"])
        assert providers == {"claude_code", "codex"}

    assert CANARY not in db.read_bytes()


def test_json_output_is_canary_clean(burn_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """G1: `--json` is the most likely thing a user pastes into an issue or pipes
    to a colleague, so it gets the same scrutiny as the database."""
    from burnometer.cli import main

    db = burn_home / "burn.db"
    _scan_fixtures_into(db)
    capsys.readouterr()

    for argv in (
        ["models", "--json"],
        ["daily", "--json"],
        ["projects", "--json"],
        ["sessions", "--json"],
        ["blocks", "--json"],
        ["today", "--json"],
    ):
        assert main(argv) == 0, argv
        out = capsys.readouterr().out
        assert "CANARY" not in out, f"content leaked via {argv}"
        assert "CANARY" not in out, f"credential-shaped value leaked via {argv}"
        assert str(Path.home()) not in out, f"home path leaked via {argv}"
        json.loads(out)  # must be valid JSON, not a rich-formatted table


def test_json_output_never_fuses_cost_bases(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A downstream consumer must inherit the rule, not have to know about it."""
    from burnometer.cli import main

    _scan_fixtures_into(burn_home / "burn.db")
    capsys.readouterr()

    assert main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "subtotals" in payload
    assert isinstance(payload["subtotals"], dict), "subtotals must be keyed by basis"
    assert "total_cost_usd" not in payload, "no fused total may be exposed"
    for row in payload["rows"]:
        assert "cost_basis" in row, "every row must declare how to read its cost"


def test_snapshot_file_is_canary_clean_and_private(burn_home: Path) -> None:
    """G1/G4: the payload the menu bar reads carries only aggregates, and is
    owner-readable only — it sits in the same directory as the database."""
    import stat as stat_module

    from burnometer.snapshot import write_snapshot
    from burnometer.store import Store

    db = burn_home / "burn.db"
    _scan_fixtures_into(db)
    with Store.open(db, read_only=True) as store:
        pass
    with Store.open(db) as store:
        path = write_snapshot(store)

    blob = path.read_bytes()
    assert CANARY not in blob, "prompt content leaked into the UI payload"
    assert CREDENTIAL_CANARY not in blob
    assert str(Path.home()).encode() not in blob, "absolute home path in the UI payload"
    assert stat_module.S_IMODE(path.stat().st_mode) == 0o600


def test_claude_plan_usage_org_id_never_reaches_the_store(burn_home: Path) -> None:
    """G1: the plan-usage file carries an account identifier we deliberately do
    not read. It must not appear anywhere in the database or the UI payload."""
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.claude_desktop import ClaudeDesktopAdapter
    from burnometer.scan import scan
    from burnometer.snapshot import write_snapshot
    from burnometer.store import Store

    fixtures = Path(__file__).parent / "fixtures" / "claude_desktop"
    assert b"CANARY-ORG-ID" in (fixtures / "plan-usage-history.json").read_bytes()

    class Scoped(ClaudeDesktopAdapter):
        def sources(self):
            return [LogSource(root=fixtures, glob="plan-usage-history.json")]

    db = burn_home / "burn.db"
    with Store.open(db) as store:
        report = scan(store, adapters=[Scoped()])
        assert report.quotas_new > 0, "fixture should produce readings"
        payload = write_snapshot(store)

    for path in (db, payload):
        assert b"CANARY" not in path.read_bytes(), f"account id leaked into {path.name}"


def test_plan_usage_adapter_cannot_reach_the_cookie_store(burn_home: Path) -> None:
    """G2: the plan-usage file sits in the same directory as Claude's Cookies
    database. The glob names one file and must never widen."""
    from burnometer.adapters.claude_desktop import ClaudeDesktopAdapter

    source = ClaudeDesktopAdapter().sources()[0]
    assert source.glob == "plan-usage-history.json", "glob must name exactly one file"
    assert "*" not in source.glob


# ------------------------------------------- G2: key-level, not just file ----
#
# File-level checks miss a store that keeps a credential *beside* the data. A
# VS Code-style state.vscdb holds an OAuth token, the account holder's name and
# email, and application state in one table: opening the file is legitimate,
# reading one of its rows is not.


@pytest.mark.parametrize(
    "key",
    [
        "antigravityUnifiedStateSync.oauthToken",
        "antigravityAuthStatus",
        "some.product.accessToken",
        "app.refreshToken",
        "settings.apiKey",
        "x.api_key",
        "vault.secret",
        "user.password",
        "auth.credentials",
        "session.cookie",
        "signing.privateKey",
    ],
)
def test_credential_keys_are_recognised(key: str) -> None:
    assert safety.is_credential_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "antigravityUnifiedStateSync.userStatus",
        "user.email",
        "account.displayName",
        "profile.avatar",
    ],
)
def test_identity_keys_are_recognised(key: str) -> None:
    """Not credentials, but nothing a usage meter needs — and every identifier
    stored is a liability."""
    assert safety.is_credential_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "antigravityUnifiedStateSync.modelCredits",
        "usage.totalTokens",
        "quota.remaining",
        "workbench.theme",
    ],
)
def test_ordinary_keys_are_allowed(key: str) -> None:
    assert safety.is_credential_key(key) is False


def test_select_keys_takes_only_what_was_asked_for() -> None:
    """Enumerate-and-filter is the wrong shape: a key added upstream would then
    be read by default, and the deny-list would have to have anticipated it."""
    available = ["a.modelCredits", "a.oauthToken", "a.userStatus", "a.theme"]
    assert safety.select_keys(available, ["a.modelCredits"]) == ["a.modelCredits"]
    assert safety.select_keys(available, ["a.notPresent"]) == []


def test_an_adapter_asking_for_a_credential_fails_loudly() -> None:
    """A mistake in an adapter must not silently widen what gets read."""
    with pytest.raises(CredentialAccessBlocked, match="oauthToken"):
        safety.select_keys(["a.oauthToken"], ["a.modelCredits", "a.oauthToken"])


def test_key_check_survives_naming_style() -> None:
    """These stores use long dotted, hyphenated and snake-cased names."""
    for variant in ("oauth_token", "OAuth-Token", "oauthToken", "x.y.OAUTH_TOKEN"):
        assert safety.is_credential_key(variant) is True
