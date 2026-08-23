"""Codex adapter: delta accounting, subset normalisation and exact quota.

The fixture in ``session.jsonl`` encodes the trap this adapter exists to avoid:
Codex repeats its final ``token_count`` event, so the provider's own
``last_token_usage`` gets counted twice by anything that sums it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from burnometer.adapters.codex import CodexAdapter
from burnometer.models import QuotaSource

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


@pytest.fixture
def adapter() -> CodexAdapter:
    return CodexAdapter()


@pytest.fixture
def session(adapter: CodexAdapter):
    return adapter.parse(FIXTURES / "session.jsonl", FIXTURES)


# -- the central accounting decision ---------------------------------------


def test_deltas_not_last_token_usage(session) -> None:
    """Totals must come from differencing the running total.

    The fixture's third token_count repeats the second verbatim. Differencing
    yields zero for it; summing ``last_token_usage`` would add another turn's
    worth of tokens that were never consumed.
    """
    assert len(session.events) == 3
    assert session.duplicates_dropped == 1

    total_input = sum(
        e.tokens.input + e.tokens.cache_read + e.tokens.cache_write_5m for e in session.events
    )
    assert total_input == 5000, "must equal the session's own final input_tokens"


def test_summing_last_token_usage_would_overcount(session) -> None:
    """Quantify what the naive reading costs, so the choice is not folklore."""
    naive = 0
    for line in (FIXTURES / "session.jsonl").read_bytes().splitlines():
        record = json.loads(line)
        payload = record.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        naive += ((payload.get("info") or {}).get("last_token_usage") or {}).get("input_tokens", 0)

    ours = sum(
        e.tokens.input + e.tokens.cache_read + e.tokens.cache_write_5m for e in session.events
    )
    assert naive == 7000, "the duplicate event contributes a phantom turn"
    assert ours == 5000
    assert naive > ours


def test_integrity_check_reconciles(session) -> None:
    """The format lets us verify our own parse; that check must pass."""
    assert session.integrity_checks == 1
    assert session.integrity_failures == 0


# -- subset normalisation --------------------------------------------------


def test_counters_are_split_into_disjoint_buckets(session) -> None:
    """``cached`` and ``cache_write`` sit inside ``input_tokens``; adding them
    would double-count. The split must re-add to the original."""
    first = session.events[0].tokens
    assert first.cache_read == 800
    assert first.input == 200, "1000 reported minus 800 cached"
    assert first.input + first.cache_read + first.cache_write_5m == 1000


def test_reasoning_is_kept_as_a_subset_of_output(session) -> None:
    first = session.events[0].tokens
    assert first.output == 100
    assert first.reasoning == 50
    assert first.total == first.input + first.output + first.cache_read + first.cache_write
    assert first.reasoning not in (first.total - first.output, first.total)


def test_no_one_hour_cache_bucket_for_openai(session) -> None:
    """OpenAI caching has no TTL split, so inventing a 1-hour bucket would
    invite the 2x Anthropic multiplier to be applied to it."""
    assert all(e.tokens.cache_write_1h == 0 for e in session.events)


# -- attribution -----------------------------------------------------------


def test_model_follows_turn_context_not_session_meta(session) -> None:
    """Sessions switch models mid-flight; pricing the whole session as one
    model would misprice every turn after the switch."""
    assert [e.model for e in session.events] == [
        "gpt-5.6-terra",
        "gpt-5.6-terra",
        "gpt-5.5",
    ]


def test_effort_is_captured_as_a_sub_dimension(session) -> None:
    assert [e.effort for e in session.events] == ["medium", "medium", "xhigh"]


def test_model_family_groups_but_preserves_the_slug(session) -> None:
    e = session.events[0]
    assert e.model == "gpt-5.6-terra"
    assert e.model_family == "gpt-5"


def test_event_keys_are_stable_and_unique(adapter: CodexAdapter) -> None:
    a = adapter.parse(FIXTURES / "session.jsonl", FIXTURES)
    b = adapter.parse(FIXTURES / "session.jsonl", FIXTURES)
    keys_a = [e.event_key for e in a.events]
    assert keys_a == [e.event_key for e in b.events], "rescans must not shift keys"
    assert len(set(keys_a)) == len(keys_a)


def test_project_path_is_reduced(session) -> None:
    assert session.events[0].project == "codex-repo"
    assert all("bigcorp" not in (e.project or "") for e in session.events)


# -- resilience ------------------------------------------------------------


def test_counter_reset_is_carried_forward(adapter: CodexAdapter) -> None:
    """A restarted counter must not produce negative deltas, and the integrity
    check must account for what came before rather than reporting a failure."""
    r = adapter.parse(FIXTURES / "reset.jsonl", FIXTURES)
    assert len(r.events) == 4
    assert all(t >= 0 for e in r.events for t in (e.tokens.input, e.tokens.output))
    assert r.integrity_failures == 0, "a reset is a known condition, not a parse bug"

    consumed = sum(e.tokens.input + e.tokens.cache_read + e.tokens.cache_write_5m for e in r.events)
    assert consumed == 8000 + 2500, "pre-reset total plus post-reset total"


def test_malformed_block_is_skipped_not_treated_as_a_reset(adapter: CodexAdapter) -> None:
    """An unreadable block yields zeros. Mistaking that for a restart would
    zero the baseline and corrupt every delta after it."""
    r = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES)
    assert r.integrity_failures == 0
    assert r.lines_skipped >= 2
    assert len(r.events) == 1


# -- quota -----------------------------------------------------------------


def test_quota_is_exact_and_complete(session) -> None:
    """Codex is the only provider that writes real rate-limit numbers to disk —
    no credentials, no network."""
    assert len(session.quotas) == 1
    q = session.quotas[0]
    assert q.source is QuotaSource.EXACT
    assert q.is_exact
    assert q.used_percent == 44.0
    assert q.window_minutes == 43200
    assert q.plan_type == "go"
    assert q.resets_at is not None and q.resets_at.year == 2026


def test_empty_quota_block_is_not_a_reading_of_zero(adapter: CodexAdapter) -> None:
    """`primary: {}` means the provider told us nothing. Recording 0% would
    claim the user has their whole quota left."""
    r = adapter.parse(FIXTURES / "edge.jsonl", FIXTURES)
    assert r.quotas == []


def test_plan_type_signals_a_subscription(session) -> None:
    """Its presence proves the user is not billed per token, which drives the
    api_equivalent labelling downstream."""
    from burnometer.scan import _detect_subscription

    assert _detect_subscription(session) is True
