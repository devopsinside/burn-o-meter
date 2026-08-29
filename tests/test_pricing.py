"""Pricing: the resolution chain, the cost formula, and refusing to invent numbers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from burnometer.models import CostBasis, TokenCounts, UsageEvent
from burnometer.pricing.calculator import compute_cost, price_event, resolve_basis
from burnometer.pricing.catalog import Catalog, Price, load_catalog, refresh_snapshot

# -- the shipped catalog ---------------------------------------------------


def test_shipped_catalog_loads_with_both_layers() -> None:
    cat = load_catalog(user_path=Path("/nonexistent"))
    assert len(cat) > 50
    assert any("models.dev" in layer for layer in cat.layers)
    assert any("overlay" in layer for layer in cat.layers)


def test_overlay_supplies_the_one_hour_cache_write_rate() -> None:
    """models.dev publishes only the 5-minute rate. Without the overlay, every
    Claude Code cache write would be priced 37.5% too low."""
    cat = load_catalog(user_path=Path("/nonexistent"))
    opus = cat.get("claude-opus-5")
    assert opus is not None
    assert opus.cache_write_5m == pytest.approx(6.25), "1.25x base input"
    assert opus.cache_write_1h == pytest.approx(10.0), "2.0x base input"
    assert opus.cache_write_1h == pytest.approx(opus.input * 2.0)


def test_provenance_names_every_contributing_layer() -> None:
    cat = load_catalog(user_path=Path("/nonexistent"))
    src = cat.get("claude-opus-5").source
    assert "models.dev@" in src
    assert "overlay@" in src and "cache_write_1h" in src


def test_unknown_model_is_unpriced_not_guessed() -> None:
    """Pricing an unrecognised model like a similar one would invent a number."""
    cat = load_catalog(user_path=Path("/nonexistent"))
    assert cat.get("claude-opus-6-does-not-exist") is None
    assert cat.get("") is None


# -- layering --------------------------------------------------------------


@pytest.fixture
def layered(tmp_path: Path) -> tuple[Path, Path, Path]:
    snap = tmp_path / "snapshot.json"
    snap.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "models": {
                    "test-model": {
                        "vendor": "test",
                        "input": 4.0,
                        "output": 20.0,
                        "cache_read": 0.4,
                        "cache_write_5m": 5.0,
                    }
                },
            }
        )
    )
    overlay = tmp_path / "overlay.toml"
    overlay.write_text('[models."test-model"]\ncache_write_1h = 8.0\nverified = "2026-02-02"\n')
    user = tmp_path / "pricing.toml"
    user.write_text('[models."test-model"]\ninput = 1.0\n')
    return snap, overlay, user


def test_overlay_patches_one_field_and_inherits_the_rest(layered) -> None:
    snap, overlay, _ = layered
    cat = load_catalog(snapshot_path=snap, overlay_path=overlay, user_path=Path("/nope"))
    p = cat.get("test-model")
    assert p.input == 4.0, "untouched fields inherit from the snapshot"
    assert p.cache_write_1h == 8.0, "overlay supplied only this"


def test_user_override_wins(layered) -> None:
    """An enterprise or discounted rate must beat everything we ship."""
    snap, overlay, user = layered
    cat = load_catalog(snapshot_path=snap, overlay_path=overlay, user_path=user)
    p = cat.get("test-model")
    assert p.input == 1.0
    assert p.cache_write_1h == 8.0, "user override must not discard the overlay"
    assert "user(input)" in p.source


# -- the cost formula ------------------------------------------------------


def test_exact_cost_for_a_known_vector() -> None:
    price = Price(input=5.0, output=25.0, cache_read=0.5, cache_write_5m=6.25, cache_write_1h=10.0)
    tokens = TokenCounts(
        input=1_000_000,
        output=1_000_000,
        cache_read=1_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
    )
    usd, _ = compute_cost(tokens, price)
    assert usd == pytest.approx(5.0 + 25.0 + 0.5 + 6.25 + 10.0)


def test_one_hour_writes_bill_at_double_not_1_25x() -> None:
    """The single most consequential assertion in this suite."""
    price = Price(input=5.0, output=0.0, cache_write_5m=6.25, cache_write_1h=10.0)
    one_hour = TokenCounts(cache_write_1h=1_000_000)
    five_min = TokenCounts(cache_write_5m=1_000_000)

    hour_usd, _ = compute_cost(one_hour, price)
    min_usd, _ = compute_cost(five_min, price)

    assert hour_usd == pytest.approx(10.0)
    assert min_usd == pytest.approx(6.25)
    assert hour_usd / min_usd == pytest.approx(2.0 / 1.25)
    # What a blended-rate tool would report, and by how much it is short.
    assert (hour_usd - min_usd) / hour_usd == pytest.approx(0.375)


def test_missing_one_hour_rate_falls_back_and_says_so() -> None:
    """Assuming a 2x premium we have no source for would be inventing a number."""
    price = Price(input=2.0, output=12.0, cache_write_5m=2.5, cache_write_1h=None)
    usd, note = compute_cost(TokenCounts(cache_write_1h=1_000_000), price)
    assert usd == pytest.approx(2.5), "falls back to the 5-minute rate"
    assert "1h cache-write rate unknown" in note


def test_reasoning_tokens_are_not_billed_twice() -> None:
    """reasoning is a subset of output; charging it again would inflate Codex."""
    price = Price(input=1.0, output=10.0)
    with_reasoning = TokenCounts(output=1_000_000, reasoning=500_000)
    without = TokenCounts(output=1_000_000)
    assert compute_cost(with_reasoning, price)[0] == compute_cost(without, price)[0]


def test_long_context_tier_applies_above_threshold() -> None:
    """OpenAI roughly doubles rates above 272k input tokens."""
    price = Price(
        input=2.0,
        output=12.0,
        cache_read=0.2,
        tier_threshold=272_000,
        tier=Price(input=4.0, output=18.0, cache_read=0.4),
    )
    small, _ = compute_cost(TokenCounts(input=100_000, output=1_000), price)
    assert small == pytest.approx((100_000 * 2.0 + 1_000 * 12.0) / 1e6)

    big, note = compute_cost(TokenCounts(input=300_000, output=1_000), price)
    assert big == pytest.approx((300_000 * 4.0 + 1_000 * 18.0) / 1e6)
    assert "long-context tier" in note


def test_tier_threshold_measured_on_the_whole_input_side() -> None:
    """Cache reads count toward context size — they are part of the prompt."""
    price = Price(
        input=2.0,
        output=12.0,
        cache_read=0.2,
        tier_threshold=272_000,
        tier=Price(input=4.0, output=18.0, cache_read=0.4),
    )
    _, note = compute_cost(TokenCounts(input=1_000, cache_read=300_000), price)
    assert "long-context tier" in note


# -- honesty ---------------------------------------------------------------


def _event(model: str) -> UsageEvent:
    return UsageEvent(
        event_key="k",
        provider="claude_code",
        model=model,
        ts=datetime(2026, 8, 21, tzinfo=UTC),
        tokens=TokenCounts(input=1000, output=1000),
    )


def test_unpriced_event_gets_null_not_zero() -> None:
    cat = load_catalog(user_path=Path("/nonexistent"))
    e = price_event(_event("totally-unknown-model"), cat)
    assert e.cost_usd is None, "0.0 would claim the request was free"
    assert e.cost_basis is CostBasis.UNPRICED
    assert "no price" in e.price_source


def test_subscription_usage_is_labelled_equivalent_not_billed() -> None:
    cat = load_catalog(user_path=Path("/nonexistent"))
    e = price_event(_event("claude-opus-5"), cat, subscription=True)
    assert e.cost_basis is CostBasis.API_EQUIVALENT
    assert e.cost_usd is not None and e.cost_usd > 0


def test_api_key_usage_is_labelled_billed() -> None:
    cat = load_catalog(user_path=Path("/nonexistent"))
    e = price_event(_event("claude-opus-5"), cat, subscription=False)
    assert e.cost_basis is CostBasis.API_BILLED


def test_undetectable_billing_defaults_to_equivalent() -> None:
    """'What this would have cost' is true either way. 'What you were charged'
    is not, so it is never the default."""
    assert resolve_basis("claude_code", subscription=None) is CostBasis.API_EQUIVALENT


# -- refresh (no real network: urlopen is stubbed) -------------------------


def test_refresh_normalises_upstream_shape(tmp_path: Path, monkeypatch) -> None:
    """Storing our own schema means an upstream format change breaks this one
    function loudly, instead of silently mispricing everything."""
    payload = {
        "anthropic": {
            "models": {
                "claude-x": {
                    "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
                    "limit": {"context": 200000},
                }
            }
        },
        "openai": {
            "models": {
                "gpt-x": {
                    "cost": {
                        "input": 2.0,
                        "output": 12.0,
                        "tiers": [{"tier": {"size": 272000}}],
                        "context_over_200k": {"input": 4.0, "output": 18.0},
                    }
                }
            }
        },
        "ignored-vendor": {"models": {"nope": {"cost": {"input": 1.0, "output": 1.0}}}},
        "google": {"models": {"no-price": {"cost": {}}}},
    }

    class FakeResponse:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())

    dest = tmp_path / "snap.json"
    snap = refresh_snapshot(dest, vendors=("anthropic", "openai", "google"))

    assert snap["models"]["claude-x"]["cache_write_5m"] == 3.75
    assert snap["models"]["gpt-x"]["tier_threshold"] == 272000
    assert snap["models"]["gpt-x"]["tier"]["input"] == 4.0
    assert "nope" not in snap["models"], "vendor filter applied"
    assert "no-price" not in snap["models"], "a model without a price is never invented"
    assert dest.exists()


def test_catalog_get_is_case_insensitive_but_not_fuzzy() -> None:
    cat = Catalog(prices={"abc-1": Price(input=1.0, output=2.0)}, layers=[])
    assert cat.get("ABC-1") is not None
    assert cat.get("abc") is None, "prefix must not match"


def test_refresh_targets_the_user_directory_not_the_package(burn_home) -> None:
    """pipx and uv install into locations the user cannot write to, so a refresh
    that targeted the packaged snapshot would fail for the two installation
    methods the README recommends."""
    from burnometer.pricing.catalog import active_snapshot_path, user_snapshot_path

    target = user_snapshot_path()
    assert str(target).startswith(str(burn_home))
    assert "site-packages" not in str(target)
    # With nothing refreshed yet, the packaged snapshot is what is in force.
    assert active_snapshot_path() != target
    assert active_snapshot_path().exists()


def test_a_refreshed_snapshot_takes_precedence(burn_home, monkeypatch) -> None:
    import json as _json

    from burnometer.pricing.catalog import (
        active_snapshot_path,
        refresh_snapshot,
        user_snapshot_path,
    )

    payload = {"anthropic": {"models": {"claude-test": {"cost": {"input": 9.0, "output": 9.0}}}}}

    class FakeResponse:
        def read(self):
            return _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    refresh_snapshot(vendors=("anthropic",))

    assert user_snapshot_path().exists()
    assert active_snapshot_path() == user_snapshot_path()
    assert load_catalog(user_path=Path("/nonexistent")).get("claude-test") is not None


def test_refreshed_snapshot_is_owner_only(burn_home, monkeypatch) -> None:
    import json as _json
    import stat as stat_module

    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    class FakeResponse:
        def read(self):
            return _json.dumps({"anthropic": {"models": {}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    refresh_snapshot(vendors=("anthropic",))
    assert stat_module.S_IMODE(user_snapshot_path().stat().st_mode) == 0o600


def test_overlay_matches_the_published_multipliers() -> None:
    """The overlay exists because no public database records the 1-hour
    cache-write rate — which means nothing upstream would catch a typo in it.
    This is that check: every entry must be the model's input rate times the
    published multiplier."""
    from burnometer.pricing.catalog import verify_overlay_multipliers

    problems = verify_overlay_multipliers()
    assert not problems, "overlay drifted from the published rates:\n" + "\n".join(problems)


def test_the_multiplier_check_actually_catches_drift(tmp_path: Path) -> None:
    """Guard the guard: a wrong overlay value must fail, or the test above is
    decorative."""
    from burnometer.pricing.catalog import verify_overlay_multipliers

    snap = tmp_path / "snapshot.json"
    snap.write_text(
        json.dumps({"models": {"m": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25}}})
    )
    overlay = tmp_path / "overlay.toml"
    # 9.0 instead of the correct 10.0 (2.0 x 5.0)
    overlay.write_text('[models."m"]\ncache_write_1h = 9.0\nverified = "2026-01-01"\n')

    cat = load_catalog(snapshot_path=snap, overlay_path=overlay, user_path=Path("/nope"))
    problems = verify_overlay_multipliers(cat)
    assert problems and "expected 10.0" in problems[0]


def test_a_zero_rate_is_not_a_price_of_zero() -> None:
    """Providers publish 0 for plan-included models. Reporting $0.00 would tell
    a user their work was free; it was covered by a subscription."""
    from burnometer.pricing.calculator import is_not_metered

    assert is_not_metered(Price(input=0.0, output=0.0)) is True
    assert is_not_metered(Price(input=5.0, output=25.0)) is False
    assert is_not_metered(Price(input=0.0, output=25.0)) is False, "only both at zero"


def test_plan_included_model_is_not_metered() -> None:
    cat = Catalog(prices={"plan-model": Price(input=0.0, output=0.0)}, layers=[])
    e = price_event(_event("plan-model"), cat, subscription=True)
    assert e.cost_basis is CostBasis.NOT_METERED
    assert e.cost_usd is None, "$0.00 would claim it was free"
    assert "no per-token rate" in e.price_source


def test_not_metered_is_distinct_from_unpriced() -> None:
    """One means we do not know the rate; the other means there is no rate.
    Collapsing them would lose a real distinction."""
    cat = Catalog(prices={"plan-model": Price(input=0.0, output=0.0)}, layers=[])
    metered = price_event(_event("no-such-model"), cat)
    planned = price_event(_event("plan-model"), cat)
    assert metered.cost_basis is CostBasis.UNPRICED
    assert planned.cost_basis is CostBasis.NOT_METERED
    assert metered.cost_usd is None and planned.cost_usd is None


def test_local_models_are_not_metered_not_unpriced():
    """ "We do not know the rate" and "there is no rate" are different claims.

    A local model has no catalog entry and never will, so falling through to
    UNPRICED would report ignorance where the truth is that the user's own
    hardware served the tokens. Both render without a dollar figure; only one is
    honest about why.
    """
    from datetime import UTC, datetime

    from burnometer.models import CostBasis, TokenCounts, UsageEvent
    from burnometer.pricing import load_catalog
    from burnometer.pricing.calculator import price_event

    catalog = load_catalog()
    local = UsageEvent(
        event_key="k",
        provider="opencode",
        upstream_provider="ollama",
        model="qwen3:0.6b",
        ts=datetime.now(UTC),
        tokens=TokenCounts(input=2050, output=130),
    )
    priced = price_event(local, catalog)
    assert priced.cost_basis is CostBasis.NOT_METERED
    assert priced.cost_usd is None, "never a dollar figure, not even zero"
    assert "ollama" in (priced.price_source or "")


def test_an_unknown_hosted_model_stays_unpriced():
    """The contrast that gives the previous test meaning.

    Same missing catalog entry, but served by someone else — so the honest answer
    is that we do not know the rate, not that none exists.
    """
    from datetime import UTC, datetime

    from burnometer.models import CostBasis, TokenCounts, UsageEvent
    from burnometer.pricing import load_catalog
    from burnometer.pricing.calculator import price_event

    hosted = UsageEvent(
        event_key="k2",
        provider="opencode",
        upstream_provider="opencode",
        model="hy3-free",
        ts=datetime.now(UTC),
        tokens=TokenCounts(input=100, output=10),
    )
    priced = price_event(hosted, load_catalog())
    assert priced.cost_basis is CostBasis.UNPRICED
    assert priced.cost_usd is None


def test_local_detection_is_case_and_whitespace_tolerant():
    """Provider ids come from a config file a human wrote."""
    from burnometer.pricing.calculator import is_local_provider

    assert is_local_provider("Ollama")
    assert is_local_provider("  ollama  ")
    assert is_local_provider("LM-Studio")
    assert not is_local_provider("openai")
    assert not is_local_provider(None)
    assert not is_local_provider("")


def test_the_packaged_snapshot_covers_every_vendor_it_claims_to() -> None:
    """`DEFAULT_VENDORS` names who gets vendored; the file has to match.

    It did not. `moonshotai`, `zhipuai`, `alibaba`, `minimax` and the inference
    hosts were added to the tuple, but the snapshot was not regenerated — so it
    shipped 132 models from six vendors while the list named sixteen, and a fresh
    install could not price Kimi, GLM or Qwen at all. Nothing failed, because
    every test that touched the catalog ran on a machine with a refreshed copy.
    """
    import json

    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT, DEFAULT_VENDORS

    models = json.loads(_PACKAGED_SNAPSHOT.read_text())["models"]
    present = {m.get("vendor") for m in models.values()}

    # Not every vendor publishes rates for every model — a vendor legitimately
    # contributes nothing when models.dev has no cost for any of its entries.
    # `ollama-cloud` is the known case: local runtimes are listed so their names
    # resolve, and most carry no price, which is the right answer for self-hosted.
    expected = set(DEFAULT_VENDORS) - {"ollama-cloud"}
    missing = expected - present
    assert not missing, (
        f"the packaged snapshot has no models for {sorted(missing)} — "
        "regenerate it with refresh_snapshot() after changing DEFAULT_VENDORS"
    )


def test_the_packaged_snapshot_can_price_a_model_from_every_agent_we_support() -> None:
    """A shipped adapter whose models cannot be priced is half a feature."""
    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT, load_catalog

    catalog = load_catalog(snapshot_path=_PACKAGED_SNAPSHOT, user_path=None)
    for agent, slug in (
        ("Claude Code", "claude-opus-5"),
        ("Codex", "gpt-5.5"),
        ("Kimi Code", "kimi-k2-turbo-preview"),
        ("OpenCode → GLM", "glm-4.6"),
    ):
        assert catalog.get(slug) is not None, f"{agent}: {slug} has no rate in the shipped snapshot"


def test_the_documented_model_count_matches_what_ships() -> None:
    """The docs stated 290 while the snapshot held 132.

    The number came from a refreshed copy on the author's machine, so it was
    true where it was written and false for every reader.
    """
    import json
    import re
    from pathlib import Path

    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT

    shipped = len(json.loads(_PACKAGED_SNAPSHOT.read_text())["models"])
    root = Path(__file__).resolve().parent.parent

    for name in ("ROADMAP.md", "docs/adding-an-agent.md"):
        text = (root / name).read_text()
        for claimed in re.findall(r"(\d{2,4}) models\b", text):
            assert int(claimed) == shipped, (
                f"{name} claims {claimed} models; the packaged snapshot has {shipped}"
            )
