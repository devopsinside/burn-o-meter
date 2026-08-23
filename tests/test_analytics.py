"""Rollups: the aggregation honesty rules, and the windowing logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from burnometer.analytics import DIMENSIONS, aggregate, blocks
from burnometer.models import CostBasis, TokenCounts, UsageEvent
from burnometer.report import format_cost, parse_since
from burnometer.store import Store

BASE = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def _ev(
    key,
    *,
    model="claude-opus-5",
    cost=1.0,
    basis=CostBasis.API_BILLED,
    minutes=0,
    project="proj",
    provider="claude_code",
    tokens=None,
) -> UsageEvent:
    return UsageEvent(
        event_key=key,
        provider=provider,
        model=model,
        ts=BASE + timedelta(minutes=minutes),
        tokens=tokens or TokenCounts(input=100, output=200, cache_read=700),
        project=project,
        cost_usd=cost,
        cost_basis=basis,
        price_source="test",
    )


# -- the honesty rules -----------------------------------------------------


def test_bases_are_never_fused(store: Store) -> None:
    """A subscription figure and a real charge must not be added together."""
    store.upsert_events(
        [
            _ev("a", cost=10.0, basis=CostBasis.API_BILLED),
            _ev("b", cost=5.0, basis=CostBasis.API_EQUIVALENT, minutes=1),
        ]
    )
    report = aggregate(store, "model")

    assert set(report.subtotals) == {CostBasis.API_BILLED, CostBasis.API_EQUIVALENT}
    assert report.subtotals[CostBasis.API_BILLED].cost_usd == 10.0
    assert report.subtotals[CostBasis.API_EQUIVALENT].cost_usd == 5.0
    assert not hasattr(report, "total_cost"), "no fused total may exist to reach for"


def test_same_model_splits_by_basis(store: Store) -> None:
    store.upsert_events(
        [
            _ev("a", cost=10.0, basis=CostBasis.API_BILLED),
            _ev("b", cost=5.0, basis=CostBasis.API_EQUIVALENT, minutes=1),
        ]
    )
    rows = aggregate(store, "model").rows
    assert len(rows) == 2
    assert {r.basis for r in rows} == {CostBasis.API_BILLED, CostBasis.API_EQUIVALENT}


def test_unpriced_kept_with_null_cost_not_zero(store: Store) -> None:
    store.upsert_events(
        [
            _ev("a", cost=10.0),
            _ev("u", model="mystery-model", cost=None, basis=CostBasis.UNPRICED, minutes=1),
        ]
    )
    report = aggregate(store, "model")

    unpriced = next(r for r in report.rows if r.key == "mystery-model")
    assert unpriced.totals.cost_usd is None, "0.0 would claim it was free"
    assert unpriced.totals.tokens.total > 0, "tokens are still reported"
    assert unpriced.is_unpriced
    assert report.unpriced_models == {"mystery-model"}


def test_share_is_computed_within_a_basis(store: Store) -> None:
    """A row's share must not be diluted by spend of a different kind."""
    store.upsert_events(
        [
            _ev("a", cost=30.0, basis=CostBasis.API_BILLED),
            _ev("b", model="claude-fable-5", cost=10.0, basis=CostBasis.API_BILLED, minutes=1),
            _ev("c", cost=1000.0, basis=CostBasis.API_EQUIVALENT, minutes=2),
        ]
    )
    report = aggregate(store, "model")
    billed = [r for r in report.rows if r.basis is CostBasis.API_BILLED]
    shares = {r.key: report.share_of_basis(r) for r in billed}
    assert shares["claude-opus-5"] == pytest.approx(0.75)
    assert shares["claude-fable-5"] == pytest.approx(0.25)


# -- derived metrics -------------------------------------------------------


def test_cache_hit_rate_uses_the_input_side_only(store: Store) -> None:
    store.upsert_events([_ev("a", tokens=TokenCounts(input=100, output=9999, cache_read=900))])
    totals = aggregate(store, "model").rows[0].totals
    assert totals.cache_hit_rate == pytest.approx(0.9), "output must not dilute the rate"


def test_effective_rate_is_all_in(store: Store) -> None:
    """The number that separates models in practice: list price is misleading
    when almost all input is cache reads."""
    store.upsert_events(
        [_ev("a", cost=1.0, tokens=TokenCounts(input=0, output=0, cache_read=1_000_000))]
    )
    assert aggregate(store, "model").rows[0].totals.effective_rate == pytest.approx(1.0)


def test_effective_rate_is_none_when_unpriced(store: Store) -> None:
    store.upsert_events([_ev("u", cost=None, basis=CostBasis.UNPRICED)])
    assert aggregate(store, "model").rows[0].totals.effective_rate is None


# -- dimensions and filtering ---------------------------------------------


def test_every_dimension_works(store: Store) -> None:
    store.upsert_events([_ev("a"), _ev("b", minutes=1)])
    for dim in DIMENSIONS:
        assert aggregate(store, dim).rows, f"{dim} produced nothing"


def test_unknown_dimension_is_rejected(store: Store) -> None:
    """The grouping expression must never come from caller input."""
    with pytest.raises(ValueError):
        aggregate(store, "model; DROP TABLE usage_events")
    assert store.count_events() == 0


def test_time_filtering(store: Store) -> None:
    store.upsert_events([_ev("old", minutes=0), _ev("new", minutes=120)])
    later = aggregate(store, "model", since=BASE + timedelta(minutes=60))
    assert later.total_requests == 1
    earlier = aggregate(store, "model", until=BASE + timedelta(minutes=60))
    assert earlier.total_requests == 1


def test_provider_filtering(store: Store) -> None:
    store.upsert_events([_ev("a"), _ev("b", provider="codex", model="gpt-5.5", minutes=1)])
    assert aggregate(store, "model", provider="codex").total_requests == 1


# -- windows ---------------------------------------------------------------


def test_events_within_five_hours_form_one_window(store: Store) -> None:
    store.upsert_events([_ev(str(i), minutes=i * 60) for i in range(4)])
    report = blocks(store)
    assert len(report.blocks) == 1
    assert report.blocks[0].requests == 4


def test_a_long_gap_opens_a_new_window(store: Store) -> None:
    store.upsert_events([_ev("a", minutes=0), _ev("b", minutes=6 * 60)])
    assert len(blocks(store).blocks) == 2


def test_window_starts_at_the_observed_first_request(store: Store) -> None:
    """Not rounded to the hour. Anthropic does not document the anchor, so
    inventing a rounding rule would move the projected reset by up to 59
    minutes."""
    odd = BASE.replace(hour=9, minute=47, second=13)
    store.upsert_events(
        [
            UsageEvent(
                event_key="x",
                provider="claude_code",
                model="claude-opus-5",
                ts=odd,
                tokens=TokenCounts(input=1),
                cost_usd=1.0,
                cost_basis=CostBasis.API_BILLED,
                price_source="t",
            )
        ]
    )
    block = blocks(store).blocks[0]
    assert block.start == odd
    assert block.expires_at == odd + timedelta(hours=5)


def test_relative_to_history_needs_enough_history(store: Store) -> None:
    """With two windows there is no meaningful median, so we say nothing rather
    than something shaky."""
    store.upsert_events([_ev("a", minutes=0), _ev("b", minutes=6 * 60)])
    report = blocks(store)
    assert report.relative_to_history(report.blocks[-1]) is None


def test_relative_to_history_compares_against_the_median(store: Store) -> None:
    events = []
    for i in range(5):
        events.append(_ev(f"e{i}", cost=2.0, minutes=i * 6 * 60))
    events.append(_ev("big", cost=8.0, minutes=5 * 6 * 60))
    store.upsert_events(events)
    report = blocks(store)
    ratio = report.relative_to_history(report.blocks[-1])
    assert ratio == pytest.approx(4.0), "8.0 against a median of 2.0"


def test_blocks_report_exposes_no_percentage_of_limit(store: Store) -> None:
    """There is no denominator to compute one from, so the type must not offer
    a field that invites inventing one."""
    store.upsert_events([_ev("a")])
    block = blocks(store).blocks[0]
    assert not hasattr(block, "used_percent")
    assert not hasattr(block, "limit")


# -- formatting ------------------------------------------------------------


def test_unpriced_renders_as_a_dash_never_zero() -> None:
    assert "—" in format_cost(None)
    assert "0.00" not in format_cost(None)


def test_subscription_costs_carry_a_tilde() -> None:
    assert format_cost(4.18, CostBasis.API_EQUIVALENT).startswith("~$")
    assert format_cost(4.18, CostBasis.API_BILLED).startswith("$")


@pytest.mark.parametrize(
    "text,delta",
    [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("2w", timedelta(weeks=2))],
)
def test_parse_since_relative(text: str, delta: timedelta) -> None:
    parsed = parse_since(text)
    assert parsed is not None
    assert abs((datetime.now(UTC) - parsed) - delta) < timedelta(seconds=5)


def test_parse_since_iso_and_keywords() -> None:
    assert parse_since("2026-08-01").year == 2026
    assert parse_since("today").hour == 0
    assert parse_since(None) is None


def test_parse_since_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        parse_since("last tuesday-ish")


def test_model_filter_narrows_a_breakdown(store: Store) -> None:
    """Answers "where did THIS model's spend go" — the drill-down behind an
    expanded row."""
    store.upsert_events(
        [
            _ev("a", model="claude-opus-5", project="alpha", cost=30.0),
            _ev("b", model="claude-opus-5", project="beta", cost=10.0, minutes=1),
            _ev("c", model="claude-fable-5", project="alpha", cost=99.0, minutes=2),
        ]
    )
    scoped = aggregate(store, "project", model="claude-opus-5")
    assert {r.key for r in scoped.rows} == {"alpha", "beta"}
    assert scoped.subtotals[CostBasis.API_BILLED].cost_usd == pytest.approx(40.0)


def test_model_filter_is_bound_not_interpolated(store: Store) -> None:
    """A model name reaching the query as text would be an injection point."""
    store.upsert_events([_ev("a")])
    hostile = aggregate(store, "project", model="claude-opus-5' OR '1'='1")
    assert hostile.rows == [], "the value must be bound, matching nothing"
    assert store.count_events() == 1
