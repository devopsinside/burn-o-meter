"""Kimi Code adapter, checked against the record shapes a real session writes.

The fixture reproduces what ``kimi -p`` actually produced on this machine: a
``usage.record`` per turn, the two ``token_counting`` records that restate it, and
the ``turn.prompt`` entry that carries the user's text and must never be read.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from burnometer.adapters.kimi import PROVIDER, SESSION_GLOB, KimiAdapter
from burnometer.pricing.calculator import is_local_provider

FIXTURE = Path(__file__).parent / "fixtures" / "kimi"
LOCAL = "session_11111111-1111-4111-8111-111111111111"
HOSTED = "session_22222222-2222-4222-8222-222222222222"


def wire(session: str) -> Path:
    return next(FIXTURE.glob(f"sessions/*/{session}/agents/main/wire.jsonl"))


@pytest.fixture
def local_events():
    return KimiAdapter().parse(wire(LOCAL), FIXTURE).events


@pytest.fixture
def hosted_result():
    return KimiAdapter().parse(wire(HOSTED), FIXTURE)


def test_usage_is_per_turn_so_summing_is_correct(local_events):
    """Codex looked exactly like this and was cumulative, so it is checked.

    Three turns whose output *falls* - 276, 239, 202 - cannot be a running total.
    """
    outputs = [e.tokens.output for e in local_events]
    assert outputs == [276, 239, 202]
    assert outputs != sorted(outputs), "a cumulative series would be non-decreasing"


def test_token_counting_records_are_never_added_to_the_total(local_events):
    """They restate the usage record; summing them would nearly double everything.

    The fixture's ``measured`` values are 2326, 2289 and 2252 - each exactly
    ``inputOther + output`` for the turn it follows.
    """
    assert sum(e.tokens.input + e.tokens.output for e in local_events) == 6150 + 717
    assert len(local_events) == 3, "one event per turn, not one per token_counting record"


def test_the_restatement_identity_is_checked_not_assumed():
    r = KimiAdapter().parse(wire(LOCAL), FIXTURE)
    assert r.integrity_checks == 3
    assert r.integrity_failures == 0


def test_a_broken_restatement_is_reported_rather_than_silently_accepted(tmp_path):
    """If Kimi's wire format changes, that must surface as a failed check."""
    src = wire(LOCAL).read_text().replace('"tokens": 2326', '"tokens": 9999')
    dest = tmp_path / "sessions" / "wd_x_1" / LOCAL / "agents" / "main"
    dest.mkdir(parents=True)
    (dest / "wire.jsonl").write_text(src)

    r = KimiAdapter().parse(dest / "wire.jsonl", tmp_path)
    assert r.integrity_failures == 1, "a changed total must not pass unnoticed"
    assert len(r.events) == 3, "the events are still recorded; only the check fails"


def test_total_input_includes_both_cache_fields(hosted_result):
    """``inputOther`` is net of cache - Kimi derives it as ``inputTokens - cached``."""
    (event,) = hosted_result.events
    t = event.tokens
    assert (t.input, t.cache_read, t.cache_write_5m) == (1200, 8000, 1500)
    assert t.output == 340


def test_provider_alias_is_split_off_the_model_slug(hosted_result, local_events):
    """``Catalog.get`` is exact-match, so the slug must be the bare model id."""
    (hosted,) = hosted_result.events
    assert (hosted.model, hosted.upstream_provider) == ("kimi-k2-turbo-preview", "kimi")
    assert (local_events[0].model, local_events[0].upstream_provider) == (
        "qwen3:0.6b",
        "lmstudio",
    )


def test_a_multi_segment_model_id_keeps_its_slashes():
    """``lmstudio/openai/gpt-oss-20b`` is the ``openai/gpt-oss-20b`` model."""
    assert KimiAdapter._split_alias("lmstudio/openai/gpt-oss-20b") == (
        "openai/gpt-oss-20b",
        "lmstudio",
    )
    assert KimiAdapter._split_alias("kimi-k3") == ("kimi-k3", None)
    assert KimiAdapter._split_alias(None) == ("unknown", None)


def test_a_local_alias_reaches_the_not_metered_path(local_events):
    assert is_local_provider(local_events[0].upstream_provider) is True


def test_the_hosted_model_is_one_the_catalog_can_price(hosted_result):
    """Against the *packaged* snapshot, which is what a fresh install has.

    Written first against ``load_catalog()``, it passed here and failed on CI:
    this machine had a refreshed snapshot with 290 models while the packaged one
    carried 132 and no Moonshot vendor at all. The shipped file is the one that
    has to price Kimi.
    """
    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT, load_catalog

    (event,) = hosted_result.events
    catalog = load_catalog(snapshot_path=_PACKAGED_SNAPSHOT, user_path=None)
    assert catalog.get(event.model) is not None, (
        "the alias split must land on a slug the packaged catalog knows"
    )


def test_a_line_still_being_written_is_not_consumed(hosted_result):
    """The hosted fixture ends mid-record, as a log being appended to would.

    The offset must stop before it, so the next scan re-reads it whole rather
    than skipping the turn it belongs to.
    """
    size = wire(HOSTED).stat().st_size
    assert hosted_result.offset < size, "offset advanced past an unterminated line"
    assert wire(HOSTED).read_bytes()[hosted_result.offset :].startswith(b'{"type"')


def test_rescanning_from_the_offset_yields_nothing_new():
    a = KimiAdapter()
    first = a.parse(wire(LOCAL), FIXTURE)
    again = a.parse(wire(LOCAL), FIXTURE, offset=first.offset)
    assert again.events == []


def test_event_keys_are_stable_across_a_full_rescan():
    """They are keyed on the timestamp, not on position: scanning resumes from a
    byte offset, so a per-parse counter would restart and collide."""
    a = KimiAdapter()
    first = [e.event_key for e in a.parse(wire(LOCAL), FIXTURE).events]
    second = [e.event_key for e in a.parse(wire(LOCAL), FIXTURE).events]
    assert first == second
    assert len(set(first)) == len(first)


def test_malformed_lines_are_skipped_not_fatal(local_events):
    """The local fixture carries a line that is not JSON."""
    assert len(local_events) == 3


def test_project_comes_from_the_index_not_the_directory_slug(local_events, hosted_result):
    assert local_events[0].project == "demo"
    assert hosted_result.events[0].project == "work"


def test_events_carry_a_datetime_not_a_string(local_events):
    from datetime import datetime

    assert all(isinstance(e.ts, datetime) for e in local_events)


def test_the_glob_cannot_reach_the_config_that_holds_api_keys(tmp_path):
    """``~/.kimi-code/config.toml`` holds an ``api_key`` per configured provider."""
    (tmp_path / "config.toml").write_text('[providers.x]\napi_key = "sk-secret"\n')
    (tmp_path / "session_index.jsonl").write_text("")
    logs = tmp_path / "sessions" / "wd_a_1" / "session_a" / "logs"
    logs.mkdir(parents=True)
    (logs / "kimi-code.log").write_text("log line\n")
    agents = tmp_path / "sessions" / "wd_a_1" / "session_a" / "agents" / "main"
    agents.mkdir(parents=True)
    (agents / "wire.jsonl").write_text("")

    matched = list(tmp_path.glob(SESSION_GLOB))
    assert matched == [agents / "wire.jsonl"]


def test_the_prompt_text_is_never_read(local_events):
    """``turn.prompt`` and ``context.append_message`` carry what the user typed."""
    raw = wire(LOCAL).read_text()
    assert "SENTINEL-PROMPT-TEXT" in raw, "guard: the fixture must contain prompt text"

    # repr, not __dict__: UsageEvent uses slots, and the point is that no field
    # of the parsed event carries the text however it is serialised.
    blob = json.dumps([repr(e) for e in local_events])
    assert "SENTINEL-PROMPT-TEXT" not in blob


def test_the_fixture_is_actually_committed():
    """`.gitignore` swallowed the OpenCode fixture once; CI was red for four days."""
    for path in (FIXTURE / "session_index.jsonl", wire(LOCAL), wire(HOSTED)):
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            cwd=FIXTURE,
        )
        assert tracked.returncode == 0, f"{path.name} is not tracked by git — CI will not have it."


def test_the_adapter_is_registered():
    """Registering the class instead of an instance passed alone and failed in the
    suite, so it is asserted rather than assumed."""
    from burnometer.adapters import get_adapters

    names = [a.name for a in get_adapters()]
    assert PROVIDER in names
