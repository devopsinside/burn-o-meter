"""Chart series and cache efficiency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from burnometer.analytics import BUCKETS, cache_efficiency, time_series
from burnometer.models import CostBasis, TokenCounts, UsageEvent
from burnometer.pricing.catalog import Catalog, Price
from burnometer.store import Store

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def _ev(
    key, *, provider="claude_code", model="claude-opus-5", cost=1.0, hours_ago=0, tokens=None
) -> UsageEvent:
    return UsageEvent(
        event_key=key,
        provider=provider,
        model=model,
        ts=NOW - timedelta(hours=hours_ago),
        tokens=tokens or TokenCounts(input=100, output=200, cache_read=700, cache_write_1h=50),
        cost_usd=cost,
        cost_basis=CostBasis.API_BILLED,
        price_source="test",
    )


# -- time series -----------------------------------------------------------


def test_buckets_are_whitelisted(store: Store) -> None:
    """The bucket expression must never come from caller input."""
    with pytest.raises(ValueError):
        time_series(store, bucket="ts); DROP TABLE usage_events--")
    assert set(BUCKETS) == {"hour", "day"}


def test_splits_each_bucket_by_provider(store: Store) -> None:
    """'How much of today was Claude' is a different question from 'how much was
    today', so the split is carried rather than pre-summed."""
    store.upsert_events(
        [
            _ev("a", provider="claude_code", cost=3.0),
            _ev("b", provider="codex", model="gpt-5.5", cost=1.0),
        ]
    )
    series = time_series(store, bucket="hour", since=NOW - timedelta(hours=1))
    point = next(p for p in series.points if p.total > 0)
    assert point.by_provider == {"claude_code": 3.0, "codex": 1.0}
    assert point.total == 4.0
    assert series.providers == ["claude_code", "codex"]


def test_empty_buckets_are_filled(store: Store) -> None:
    """Dropping idle periods would compress them out of existence and make a
    burst look like steady work."""
    store.upsert_events([_ev("a", hours_ago=0), _ev("b", hours_ago=5)])
    series = time_series(store, bucket="hour", since=NOW - timedelta(hours=5))
    assert len(series.points) >= 6
    assert sum(1 for p in series.points if p.total == 0) >= 3


def test_fill_can_be_disabled(store: Store) -> None:
    store.upsert_events([_ev("a", hours_ago=0), _ev("b", hours_ago=5)])
    series = time_series(store, bucket="hour", since=NOW - timedelta(hours=5), fill=False)
    assert all(p.total > 0 for p in series.points)


def test_series_total_and_peak(store: Store) -> None:
    store.upsert_events(
        [
            _ev("a", cost=2.0, hours_ago=0),
            _ev("b", cost=5.0, hours_ago=25),
            _ev("c", cost=1.0, hours_ago=25),
        ]
    )
    series = time_series(store, bucket="day", since=NOW - timedelta(days=2))
    assert series.total == pytest.approx(8.0)
    assert series.peak == pytest.approx(6.0), "the two same-day events share a bucket"


def test_points_are_chronological(store: Store) -> None:
    store.upsert_events([_ev(str(i), hours_ago=i * 24) for i in range(4)])
    labels = [
        p.label for p in time_series(store, bucket="day", since=NOW - timedelta(days=4)).points
    ]
    assert labels == sorted(labels)


# -- cache efficiency ------------------------------------------------------


def test_hit_rate_uses_the_input_side_only(store: Store) -> None:
    store.upsert_events([_ev("a", tokens=TokenCounts(input=100, output=99_999, cache_read=900))])
    assert cache_efficiency(store).hit_rate == pytest.approx(0.9)


def test_hit_rate_is_none_with_no_data(store: Store) -> None:
    """No usage is not a 0% hit rate."""
    assert cache_efficiency(store).hit_rate is None


def test_savings_is_the_gap_between_fresh_and_cached_rates(store: Store) -> None:
    """Opus 5 reads cache at $0.50/Mtok against a $5.00 input rate, so a million
    cached tokens saved $4.50."""
    store.upsert_events([_ev("a", tokens=TokenCounts(cache_read=1_000_000))])
    saved = cache_efficiency(store).savings_usd
    assert saved == pytest.approx(4.5, abs=0.01)


def test_savings_is_withheld_when_a_model_is_unpriced(store: Store) -> None:
    """A partial figure would understate the saving and still read as fact."""
    store.upsert_events(
        [
            _ev("a", tokens=TokenCounts(cache_read=1_000_000)),
            UsageEvent(
                event_key="u",
                provider="claude_code",
                model="mystery-model",
                ts=NOW,
                tokens=TokenCounts(cache_read=500_000),
                cost_usd=None,
                cost_basis=CostBasis.UNPRICED,
                price_source="none",
            ),
        ]
    )
    assert cache_efficiency(store).savings_usd is None


def test_unpriced_model_without_cache_reads_does_not_void_the_figure(store: Store) -> None:
    """Only cached tokens contribute to the saving, so a model with none cannot
    make it incomplete."""
    store.upsert_events(
        [
            _ev("a", tokens=TokenCounts(cache_read=1_000_000)),
            UsageEvent(
                event_key="u",
                provider="claude_code",
                model="mystery-model",
                ts=NOW,
                tokens=TokenCounts(input=10, output=10),
                cost_usd=None,
                cost_basis=CostBasis.UNPRICED,
                price_source="none",
            ),
        ]
    )
    assert cache_efficiency(store).savings_usd == pytest.approx(4.5, abs=0.01)


def test_time_filtering(store: Store) -> None:
    store.upsert_events(
        [
            _ev("old", hours_ago=100, tokens=TokenCounts(cache_read=1_000_000)),
            _ev("new", hours_ago=0, tokens=TokenCounts(cache_read=10)),
        ]
    )
    recent = cache_efficiency(store, since=NOW - timedelta(hours=1))
    assert recent.cache_read == 10


def test_cache_write_counts_toward_the_input_side(store: Store) -> None:
    """Cache writes are prompt tokens too; excluding them would overstate the
    hit rate."""
    stats = cache_efficiency.__wrapped__ if hasattr(cache_efficiency, "__wrapped__") else None
    del stats
    from burnometer.analytics import CacheEfficiency

    c = CacheEfficiency(cache_read=50, cache_write_1h=50, fresh_input=0)
    assert c.input_side == 100
    assert c.hit_rate == pytest.approx(0.5)


def test_catalog_price_gap_drives_the_saving() -> None:
    """Guard the arithmetic independently of the store."""
    price = Price(input=5.0, output=25.0, cache_read=0.5)
    catalog = Catalog(prices={"m": price}, layers=[])
    assert catalog.get("m").input - catalog.get("m").cache_read == pytest.approx(4.5)


def test_uncached_counterfactual_prices_everything_at_full_rate(store: Store) -> None:
    """The number that makes the saving legible: what the same work would have
    cost with no prompt caching at all."""
    store.upsert_events([_ev("a", tokens=TokenCounts(input=0, output=0, cache_read=1_000_000))])
    c = cache_efficiency(store)
    # Opus 5: cache reads at $0.50/Mtok, the same tokens fresh at $5.00.
    assert c.actual_usd == pytest.approx(0.5, abs=0.01)
    assert c.without_cache_usd == pytest.approx(5.0, abs=0.01)
    assert c.savings_usd == pytest.approx(4.5, abs=0.01)


def test_saving_is_the_difference_between_the_two(store: Store) -> None:
    store.upsert_events(
        [
            _ev(
                "a",
                tokens=TokenCounts(
                    input=1000, output=2000, cache_read=500_000, cache_write_1h=10_000
                ),
            )
        ]
    )
    c = cache_efficiency(store)
    assert c.without_cache_usd > c.actual_usd
    assert c.savings_usd == pytest.approx(c.without_cache_usd - c.actual_usd, abs=0.01)


def test_counterfactual_is_withheld_when_a_model_is_unpriced(store: Store) -> None:
    """A partial counterfactual would understate what caching is worth."""
    store.upsert_events(
        [
            _ev("a", tokens=TokenCounts(cache_read=1_000_000)),
            UsageEvent(
                event_key="u",
                provider="claude_code",
                model="mystery-model",
                ts=NOW,
                tokens=TokenCounts(cache_read=9_000_000),
                cost_usd=None,
                cost_basis=CostBasis.UNPRICED,
                price_source="none",
            ),
        ]
    )
    c = cache_efficiency(store)
    assert c.without_cache_usd is None
    assert c.actual_usd is None
