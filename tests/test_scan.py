"""Scanner: incrementality, idempotence and fault isolation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from burnometer.adapters.base import LogSource, ParseResult
from burnometer.adapters.claude_code import ClaudeCodeAdapter
from burnometer.scan import scan
from burnometer.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"


def _adapter_over(directory: Path) -> ClaudeCodeAdapter:
    """A Claude Code adapter pointed at a temp directory instead of ``~``."""

    class Scoped(ClaudeCodeAdapter):
        def sources(self):
            return [LogSource(root=directory, glob="*.jsonl")]

    return Scoped()


@pytest.fixture
def live_logs(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    shutil.copy(FIXTURES / "basic.jsonl", d / "basic.jsonl")
    return d


def test_scan_populates_store(store: Store, live_logs: Path) -> None:
    r = scan(store, adapters=[_adapter_over(live_logs)])
    assert r.files_parsed == 1
    assert r.events_new == 2
    assert r.duplicates_dropped == 2
    assert store.count_events() == 2


def test_rescan_is_a_no_op(store: Store, live_logs: Path) -> None:
    """The menu bar polls this every few seconds; an unchanged rescan must cost
    nothing and change nothing."""
    a = _adapter_over(live_logs)
    scan(store, adapters=[a])
    second = scan(store, adapters=[a])
    assert second.files_parsed == 0
    assert second.files_unchanged == 1
    assert second.events_new == 0
    assert store.count_events() == 2


def test_appended_data_is_picked_up_incrementally(store: Store, live_logs: Path) -> None:
    a = _adapter_over(live_logs)
    scan(store, adapters=[a])
    first_lines = 0

    log = live_logs / "basic.jsonl"
    extra = (FIXTURES / "edge.jsonl").read_text().rsplit("\n", 2)[0] + "\n"
    with open(log, "a") as fh:
        fh.write(extra)

    second = scan(store, adapters=[a])
    assert second.files_parsed == 1
    assert second.lines_read > first_lines
    assert second.events_new > 0, "appended records must be picked up"
    assert store.count_events() > 2


def test_truncation_forces_a_full_reread(store: Store, live_logs: Path) -> None:
    """If a file shrinks below our saved offset, that offset now points into
    different data. Re-read from zero rather than resume into nonsense."""
    a = _adapter_over(live_logs)
    scan(store, adapters=[a])

    log = live_logs / "basic.jsonl"
    log.write_bytes((FIXTURES / "basic.jsonl").read_bytes()[:200])

    r = scan(store, adapters=[a])
    assert r.files_parsed == 1, "truncated file must be re-read, not skipped"


def test_force_rereads_everything(store: Store, live_logs: Path) -> None:
    a = _adapter_over(live_logs)
    scan(store, adapters=[a])
    forced = scan(store, adapters=[a], force=True)
    assert forced.files_parsed == 1
    assert forced.events_new == 0, "re-reading must not duplicate stored rows"


def test_unimplemented_adapter_is_skipped_not_crashed(store: Store, live_logs: Path) -> None:
    """An adapter may know where its logs live before it can parse them. That
    must show up as 'not yet supported', never as a crashed scan.

    Uses a purpose-built stub rather than a real adapter, so the test keeps
    testing the skip behaviour after that adapter is implemented.
    """

    class NotYetBuilt(ClaudeCodeAdapter):
        name = "not_yet_built"
        implemented = False

        def sources(self):
            return [LogSource(root=live_logs, glob="*.jsonl")]

        def parse(self, path, root, offset=0, project_mode="basename") -> ParseResult:
            raise NotImplementedError("should never be called")

    r = scan(store, adapters=[NotYetBuilt()])
    assert r.files_parsed == 0
    assert r.errors == []
    assert store.count_events() == 0


def test_one_bad_adapter_does_not_abort_the_scan(store: Store, live_logs: Path) -> None:
    """Fault isolation: a bug in one provider must not cost the user every other
    provider's data."""

    class Exploding(ClaudeCodeAdapter):
        name = "exploding"
        implemented = True

        def sources(self):
            return [LogSource(root=live_logs, glob="*.jsonl")]

        def parse(self, path, root, offset=0, project_mode="basename") -> ParseResult:
            raise RuntimeError("CANARY-SECRET-IN-EXCEPTION-TEXT")

    good = _adapter_over(live_logs)
    r = scan(store, adapters=[Exploding(), good])

    assert r.files_failed == 1
    assert r.events_new == 2, "the healthy adapter still ran"
    joined = " ".join(r.errors)
    assert "RuntimeError" in joined
    assert "CANARY" not in joined, "exception text must not reach the report (G6)"


def test_scan_report_arithmetic(store: Store, live_logs: Path) -> None:
    r = scan(store, adapters=[_adapter_over(live_logs)])
    assert r.records_seen == r.events_found + r.duplicates_dropped
    assert r.events_already_known == r.events_found - r.events_new


def test_subscription_is_detected_from_claude_plan_usage(store: Store) -> None:
    """Claude Code's transcripts say nothing about how the account is billed.
    Utilisation readings exist only for an account on a plan, so their presence
    settles it without touching a credential."""
    from datetime import UTC, datetime

    from burnometer.models import QuotaSnapshot, QuotaSource
    from burnometer.scan import detect_subscription_in_store

    assert detect_subscription_in_store(store, "claude_code") is None

    store.record_quota(
        [
            QuotaSnapshot(
                provider="claude",
                window_name="five_hour",
                used_percent=20.0,
                observed_at=datetime.now(UTC),
                source=QuotaSource.EXACT,
                window_minutes=300,
            )
        ]
    )
    assert detect_subscription_in_store(store, "claude_code") is True


def test_codex_subscription_detected_from_plan_type(store: Store) -> None:
    from datetime import UTC, datetime

    from burnometer.models import QuotaSnapshot, QuotaSource
    from burnometer.scan import detect_subscription_in_store

    store.record_quota(
        [
            QuotaSnapshot(
                provider="codex",
                window_name="primary",
                used_percent=50.0,
                observed_at=datetime.now(UTC),
                source=QuotaSource.EXACT,
                window_minutes=10080,
                plan_type="go",
            )
        ]
    )
    assert detect_subscription_in_store(store, "codex") is True


def test_nothing_known_stays_none(store: Store) -> None:
    """Absence of evidence is not evidence of an API key — it falls back to the
    safe default rather than guessing."""
    from burnometer.scan import detect_subscription_in_store

    assert detect_subscription_in_store(store, "claude_code") is None
    assert detect_subscription_in_store(store, "codex") is None
    assert detect_subscription_in_store(store, "unknown_provider") is None


def test_config_overrides_detection(store: Store) -> None:
    """A user who pays per token must be able to say so and be believed."""
    from burnometer.config import BillingConfig

    billing = BillingConfig(claude_code="api")
    assert billing.subscription_for("claude_code", detected=True) is False


# -- where a tool's data actually lives ------------------------------------
#
# Both failures below are silent: the user sees "not installed" and no error,
# which is worse than a crash because nothing prompts them to look.


def test_env_override_wins_outright(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who set CODEX_HOME told us where their data is."""
    from burnometer.adapters.base import resolve_roots

    moved = tmp_path / "elsewhere"
    moved.mkdir()
    default = tmp_path / "default"
    default.mkdir()

    monkeypatch.setenv("SOME_TOOL_HOME", str(moved))
    assert resolve_roots("SOME_TOOL_HOME", (default,)) == [moved]


def test_env_override_is_used_even_if_it_does_not_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit setting is the answer, not a hint — silently falling back to
    a default would scan the wrong machine's worth of data."""
    from burnometer.adapters.base import resolve_roots

    monkeypatch.setenv("SOME_TOOL_HOME", str(tmp_path / "not-created-yet"))
    roots = resolve_roots("SOME_TOOL_HOME", (tmp_path,))
    assert roots == [tmp_path / "not-created-yet"]


def test_every_existing_default_is_returned(tmp_path: Path, monkeypatch) -> None:
    """Claude Code writes to ~/.claude or ~/.config/claude depending on install.
    Checking one and reporting zero for the other is worse than no support."""
    from burnometer.adapters.base import resolve_roots

    monkeypatch.delenv("SOME_TOOL_HOME", raising=False)
    a, b, missing = tmp_path / "a", tmp_path / "b", tmp_path / "missing"
    a.mkdir()
    b.mkdir()
    assert resolve_roots("SOME_TOOL_HOME", (a, missing, b)) == [a, b]


def test_claude_code_checks_both_documented_locations() -> None:
    from burnometer.adapters.claude_code import ClaudeCodeAdapter

    roots = {str(r) for r in ClaudeCodeAdapter.DEFAULT_ROOTS}
    assert any(r.endswith("/.claude") for r in roots)
    assert any(r.endswith("/.config/claude") for r in roots)
    assert ClaudeCodeAdapter.ENV_VAR == "CLAUDE_CONFIG_DIR"


def test_codex_honours_its_home_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from burnometer.adapters.codex import CodexAdapter

    sessions = tmp_path / "sessions" / "2026" / "08" / "22"
    sessions.mkdir(parents=True)
    (sessions / "rollout-x.jsonl").write_text("{}\n")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    found = [f for s in CodexAdapter().sources() for f in s.discover()]
    assert len(found) == 1


def test_every_source_records_its_env_var() -> None:
    """`doctor` needs to be able to tell a user which knob to turn."""
    from burnometer.adapters import get_adapters

    for adapter in get_adapters():
        for source in adapter.sources():
            assert hasattr(source, "env_var")


def test_derived_sources_are_reread_when_unchanged(store: Store, tmp_path: Path) -> None:
    """A source whose answer is computed from the whole series can yield a new
    result without the file changing — Claude's reset time only becomes
    computable once enough history exists. Skipping it on 'unchanged' would make
    an upgrade wait for the file to change."""

    class Derived(ClaudeCodeAdapter):
        name = "derived"
        rescan_unchanged = True

        def sources(self):
            return [LogSource(root=tmp_path, glob="*.jsonl")]

    (tmp_path / "a.jsonl").write_bytes(
        (Path(__file__).parent / "fixtures" / "claude_code" / "basic.jsonl").read_bytes()
    )
    adapter = Derived()
    scan(store, adapters=[adapter])
    again = scan(store, adapters=[adapter])
    assert again.files_parsed == 1, "a derived source must be re-read"
    assert again.files_unchanged == 0


def test_ordinary_sources_still_skip_unchanged_files(store: Store, live_logs: Path) -> None:
    """The default stays cheap — transcripts are large and append-only."""
    adapter = _adapter_over(live_logs)
    assert getattr(adapter, "rescan_unchanged", False) is False
    scan(store, adapters=[adapter])
    again = scan(store, adapters=[adapter])
    assert again.files_unchanged == 1 and again.files_parsed == 0
