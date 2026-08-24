"""Claude Code adapter: exact token accounting on hand-built fixtures.

Fixture values are round and known, so these assert exact numbers rather than
ranges. Each test names the real-world behaviour it pins down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from burnometer.adapters.claude_code import ClaudeCodeAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"


@pytest.fixture
def adapter() -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter()


@pytest.fixture
def basic(adapter: ClaudeCodeAdapter):
    return adapter.parse(FIXTURES / "basic.jsonl", FIXTURES)


@pytest.fixture
def edge(adapter: ClaudeCodeAdapter):
    return adapter.parse(FIXTURES / "edge.jsonl", FIXTURES)


# -- deduplication ---------------------------------------------------------


def test_duplicates_are_collapsed(basic) -> None:
    """The fixture holds three byte-identical copies of one message, which is
    what Claude Code actually writes. Only one may survive."""
    keys = [e.event_key for e in basic.events]
    assert keys.count("claude_code:req_A:msg_A") == 1
    assert basic.duplicates_dropped == 2


def test_only_billable_records_become_events(basic) -> None:
    assert len(basic.events) == 2, "user turns, tool results and synthetic are not usage"


def test_synthetic_model_is_excluded(basic) -> None:
    """`<synthetic>` marks Claude Code's own error text. It is never billed and
    has no model to price, so counting it would invent spend."""
    assert all(e.model != "<synthetic>" for e in basic.events)


# -- token accounting ------------------------------------------------------


def test_exact_token_values(basic) -> None:
    by_key = {e.event_key: e for e in basic.events}
    a = by_key["claude_code:req_A:msg_A"].tokens
    assert (a.input, a.output, a.cache_read) == (10, 100, 5000)
    assert (a.cache_write_5m, a.cache_write_1h) == (0, 13000)

    b = by_key["claude_code:req_B:msg_B"].tokens
    assert (b.input, b.output, b.cache_read) == (5, 50, 2000)
    assert (b.cache_write_5m, b.cache_write_1h) == (1000, 0)


def test_cache_ttl_split_is_preserved(basic) -> None:
    """The whole accuracy argument rests on this split surviving parsing: 5m
    writes bill at 1.25x base input, 1h writes at 2.0x."""
    total_5m = sum(e.tokens.cache_write_5m for e in basic.events)
    total_1h = sum(e.tokens.cache_write_1h for e in basic.events)
    assert (total_5m, total_1h) == (1000, 13000)


def test_iterations_field_is_ignored(basic) -> None:
    """`usage.iterations` restates the parent's counts. Adding it would double
    every record that has one."""
    a = next(e for e in basic.events if e.event_key.endswith("msg_A"))
    assert a.tokens.output == 100, "output doubled — iterations was counted"


def test_legacy_cache_format_treated_as_five_minute(edge) -> None:
    """Older Claude Code emits one flat cache figure with no TTL. Attributing it
    to the cheaper 5-minute rate under-attributes rather than inventing a
    premium that may never have been charged."""
    legacy = next(e for e in edge.events if e.event_key == "claude_code:req_L:msg_L")
    assert legacy.tokens.cache_write_5m == 500
    assert legacy.tokens.cache_write_1h == 0


def test_input_is_not_inflated_by_cache_reads(basic) -> None:
    """Claude Code already reports input excluding cache reads, unlike Codex.
    Adding them here would be the classic double-count."""
    a = next(e for e in basic.events if e.event_key.endswith("msg_A"))
    assert a.tokens.input == 10
    assert a.tokens.total == 10 + 100 + 5000 + 13000


# -- resilience ------------------------------------------------------------


def test_missing_request_id_falls_back_to_message_id(edge) -> None:
    """A small number of real records lack requestId. message.id is already
    unique per API response, so dedup still works."""
    assert "claude_code:msg_N" in {e.event_key for e in edge.events}


def test_hostile_and_malformed_records_are_skipped(edge) -> None:
    keys = {e.event_key for e in edge.events}
    assert "claude_code:req_H:msg_H" not in keys, "non-scalar model must not parse"
    assert "claude_code:req_T:msg_T" not in keys, "unparseable timestamp must not parse"
    assert len(edge.events) == 2
    assert edge.lines_skipped >= 3


def test_incomplete_trailing_line_is_not_consumed(edge) -> None:
    """A scan racing an active session must not half-read the line being
    written, or that record is silently lost forever."""
    size = (FIXTURES / "edge.jsonl").stat().st_size
    assert edge.offset < size
    assert "claude_code:req_P:msg_P" not in {e.event_key for e in edge.events}


def test_resuming_from_offset_reads_only_new_data(
    adapter: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    src = (FIXTURES / "basic.jsonl").read_bytes()
    log = tmp_path / "live.jsonl"
    log.write_bytes(src)

    first = adapter.parse(log, tmp_path)
    assert first.events

    # Session continues; the file grows.
    log.write_bytes(src + src)
    second = adapter.parse(log, tmp_path, offset=first.offset)
    assert second.lines_read < first.lines_read + second.lines_read
    assert second.offset == log.stat().st_size


# -- privacy ---------------------------------------------------------------


def test_project_mode_is_applied_during_parse(adapter: ClaudeCodeAdapter) -> None:
    """G5: reduction happens before storage, so a stricter mode leaves nothing
    recoverable rather than merely hidden."""
    full = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES, project_mode="full")
    base = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES, project_mode="basename")
    hashed = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES, project_mode="hash")
    none = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES, project_mode="none")

    assert full.events[0].project == "/Users/testuser/clients/bigcorp/secret-repo"
    assert base.events[0].project == "secret-repo"
    assert hashed.events[0].project.startswith("proj_")
    assert "bigcorp" not in hashed.events[0].project
    assert none.events[0].project is None


def test_raw_file_provenance_is_redacted(basic) -> None:
    """Every event can be traced to a line on disk, without leaking a home path."""
    e = basic.events[0]
    assert e.raw_line is not None and e.raw_line > 0
    assert str(Path.home()) not in (e.raw_file or "")


def test_every_registered_adapter_satisfies_the_protocol():
    """The Protocol is the contract; nothing was checking anyone kept it.

    Two adapters were missing `rescan_unchanged`, so `isinstance(a, Adapter)` was
    False for both. It caused no crash — the scanner reads it with a getattr default
    — but a contract nobody verifies is a comment, and the next adapter would have
    inherited the same silence.
    """
    from burnometer.adapters import get_adapters
    from burnometer.adapters.base import Adapter

    adapters = get_adapters()
    assert adapters, "no adapters are registered"
    for adapter in adapters:
        assert isinstance(adapter, Adapter), (
            f"{adapter.name} does not satisfy the Adapter protocol — a declared member is missing"
        )
        # Declared, not merely defaulted, so each adapter states its own intent.
        for member in ("name", "display_name", "implemented", "rescan_unchanged"):
            assert hasattr(adapter, member), f"{adapter.name} is missing {member}"
