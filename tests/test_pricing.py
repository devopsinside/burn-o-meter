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


def plausible(models: dict | None = None, vendor: str = "anthropic") -> dict:
    """An upstream payload big enough for `refresh_snapshot` to accept.

    It refuses a result below `_MIN_PLAUSIBLE_MODELS`, because persisting a short
    one is how every model on a real machine silently lost its price. Tests that
    care about *shape* still have to clear that floor, so the models they assert on
    are padded with filler rather than the floor being made tunable — a knob for
    lowering it in tests is a knob for lowering it in production.
    """
    from burnometer.pricing.catalog import _MIN_PLAUSIBLE_MODELS

    out = dict(models or {})
    filler = {
        f"filler-{i}": {"cost": {"input": 1.0, "output": 1.0}} for i in range(_MIN_PLAUSIBLE_MODELS)
    }
    return {vendor: {"models": {**filler, **out}}}


def test_refresh_normalises_upstream_shape(tmp_path: Path, monkeypatch) -> None:
    """Storing our own schema means an upstream format change breaks this one
    function loudly, instead of silently mispricing everything."""
    payload = {
        "anthropic": {
            "models": {
                **plausible()["anthropic"]["models"],
                "claude-x": {
                    "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
                    "limit": {"context": 200000},
                },
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

    from burnometer.pricing.catalog import (
        active_snapshot_path,
        refresh_snapshot,
        user_snapshot_path,
    )

    payload = plausible({"claude-test": {"cost": {"input": 9.0, "output": 9.0}}})

    class FakeResponse:
        def read(self):
            return json.dumps(payload).encode()

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
    import stat as stat_module

    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    class FakeResponse:
        def read(self):
            return json.dumps(plausible()).encode()

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
        # A model Moonshot currently sells, not one kept by retention: this asserts
        # that today's usage is priceable, which a retired slug cannot show.
        ("Kimi Code", "kimi-k3"),
        ("OpenCode → GLM", "glm-4.6"),
    ):
        assert catalog.get(slug) is not None, f"{agent}: {slug} has no rate in the shipped snapshot"


def test_the_documented_model_count_matches_what_ships() -> None:
    """The docs stated 290 while the snapshot held 132.

    The number came from a refreshed copy on the author's machine, so it was
    true where it was written and false for every reader.
    """
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


def _fake_upstream(monkeypatch, payload: dict) -> None:
    import urllib.request

    class FakeResponse:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())


def test_a_refresh_that_returns_nothing_is_refused_not_saved(burn_home, monkeypatch) -> None:
    """This happened, on a real machine, and nothing said so.

    A refresh met a bad response and wrote a snapshot with zero models. A refreshed
    snapshot shadows the packaged one, so every model on that machine became
    unpriced — the user's main model rendered as "—" with no error anywhere, and
    the only symptom was a cost that had quietly stopped existing.
    """
    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    _fake_upstream(monkeypatch, {"anthropic": {"models": {}}})
    with pytest.raises(ValueError, match="refusing to save"):
        refresh_snapshot()
    assert not user_snapshot_path().exists(), "an empty refresh must leave no file"


def test_a_failed_refresh_leaves_the_previous_snapshot_intact(burn_home, monkeypatch) -> None:
    """Failing is only better than persisting if the good file survives."""
    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    _fake_upstream(
        monkeypatch, plausible({"claude-good": {"cost": {"input": 3.0, "output": 15.0}}})
    )
    refresh_snapshot()
    before = user_snapshot_path().read_bytes()

    _fake_upstream(monkeypatch, {"anthropic": {"models": {}}})
    with pytest.raises(ValueError):
        refresh_snapshot()

    assert user_snapshot_path().read_bytes() == before, "a bad refresh overwrote a good one"


def test_an_already_written_empty_snapshot_does_not_shadow_the_packaged_one(
    burn_home, monkeypatch
) -> None:
    """The other half: refusing to write only helps machines without one already.

    A user who refreshed before the guard existed still has the bad file, so
    precedence has to be earned on every read rather than granted by existence.
    """

    from burnometer.pricing.catalog import (
        _PACKAGED_SNAPSHOT,
        active_snapshot_path,
        user_snapshot_path,
    )

    path = user_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": "2026-09-14T22:00:43+00:00", "models": {}}))

    assert active_snapshot_path() == _PACKAGED_SNAPSHOT
    assert load_catalog().get("claude-opus-5") is not None, (
        "an empty refreshed snapshot must not unprice everything"
    )


@pytest.mark.parametrize(
    "content",
    ["", "not json at all", "[]", '{"models": null}', '{"no_models_key": 1}'],
    ids=["empty", "not-json", "wrong-type", "null-models", "missing-key"],
)
def test_an_unreadable_snapshot_falls_back_rather_than_raising(burn_home, content: str) -> None:
    """A truncated write or a half-finished download must not break every command."""
    from burnometer.pricing.catalog import (
        _PACKAGED_SNAPSHOT,
        active_snapshot_path,
        user_snapshot_path,
    )

    path = user_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    assert active_snapshot_path() == _PACKAGED_SNAPSHOT
    assert len(load_catalog().prices) > 200


def test_a_healthy_refreshed_snapshot_still_wins(burn_home, monkeypatch) -> None:
    """The guard must not be so eager it ignores a legitimate refresh."""
    from burnometer.pricing.catalog import (
        active_snapshot_path,
        refresh_snapshot,
        user_snapshot_path,
    )

    _fake_upstream(
        monkeypatch, plausible({"claude-fresh": {"cost": {"input": 1.0, "output": 2.0}}})
    )
    refresh_snapshot()

    assert active_snapshot_path() == user_snapshot_path()
    assert load_catalog(user_path=Path("/nonexistent")).get("claude-fresh") is not None


def test_repricing_preserves_not_metered_for_locally_served_events(burn_home) -> None:
    """A reprice must not turn "there is no rate" into "we do not know the rate".

    `reprice` made the pricing decision itself rather than calling the function the
    scan path calls, so it never learned about `not_metered`. One
    `burn-o-meter reprice` relabelled every locally-served event `unpriced` — a
    different and wrong claim, and invisible, because both render as an em dash.

    Found on a real machine, not by a test, which is why this one exists.
    """
    from datetime import UTC, datetime

    from burnometer.models import CostBasis, TokenCounts, UsageEvent
    from burnometer.scan import reprice
    from burnometer.store import Store

    event = UsageEvent(
        event_key="local:1",
        provider="kimi",
        model="qwen3:0.6b",
        upstream_provider="ollama",
        effort=None,
        ts=datetime.now(tz=UTC),
        tokens=TokenCounts(input=100, output=50),
        session_id="s",
        project=None,
        raw_file=None,
        raw_line=0,
    )

    with Store.open(burn_home / "burn.db") as store:
        store.upsert_events([price_event(event, load_catalog())])
        assert _basis_of(store, "local:1") == CostBasis.NOT_METERED.value

        updated, _ = reprice(store)
        assert updated >= 1
        assert _basis_of(store, "local:1") == CostBasis.NOT_METERED.value, (
            "repricing downgraded a locally-served event to unpriced"
        )


def _basis_of(store, key: str) -> str:
    row = store._conn.execute(
        "SELECT cost_basis FROM usage_events WHERE event_key = ?", (key,)
    ).fetchone()
    return row["cost_basis"]


def test_repricing_and_scanning_agree_on_every_stored_event(burn_home) -> None:
    """The two paths must reach the same verdict, for every shape we store.

    Asserted as a property rather than case by case: they diverged once and the
    only symptom was a dash that meant something subtly different.
    """
    from datetime import UTC, datetime

    from burnometer.models import TokenCounts, UsageEvent
    from burnometer.scan import reprice
    from burnometer.store import Store

    catalog = load_catalog()
    cases = [
        ("claude_code", "claude-opus-5", None),  # priced
        ("kimi", "qwen3:0.6b", "ollama"),  # local -> not_metered
        ("kimi", "qwen3:0.6b", "lmstudio"),  # local -> not_metered
        ("opencode", "no-such-model-anywhere", None),  # unpriced
        ("codex", "gpt-5.5", "openai"),  # priced, remote upstream
    ]
    events = [
        price_event(
            UsageEvent(
                event_key=f"k{i}",
                provider=prov,
                model=model,
                upstream_provider=up,
                effort=None,
                ts=datetime.now(tz=UTC),
                tokens=TokenCounts(input=100, output=50),
                session_id="s",
                project=None,
                raw_file=None,
                raw_line=0,
            ),
            catalog,
        )
        for i, (prov, model, up) in enumerate(cases)
    ]
    at_scan = {e.event_key: e.cost_basis.value for e in events}

    with Store.open(burn_home / "burn.db") as store:
        store.upsert_events(events)
        reprice(store)
        after = {k: _basis_of(store, k) for k in at_scan}

    assert after == at_scan, f"reprice disagreed with scan: {after} != {at_scan}"


def test_a_model_dropped_upstream_keeps_its_rate(burn_home, monkeypatch) -> None:
    """Leaving the catalogue does not change what a model's tokens cost.

    Moonshot retired its K2 generation and models.dev stopped listing ten models.
    Dropping their rates would retroactively unprice history genuinely billed at
    them - one `reprice` and every past turn becomes an em dash.
    """
    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    old = plausible(
        {"kimi-k2-retired": {"cost": {"input": 0.6, "output": 2.5}}}, vendor="moonshotai"
    )
    _fake_upstream(monkeypatch, old)
    refresh_snapshot(vendors=("moonshotai",))

    new = plausible({"kimi-k3": {"cost": {"input": 3.0, "output": 15.0}}}, vendor="moonshotai")
    _fake_upstream(monkeypatch, new)
    snapshot = refresh_snapshot(vendors=("moonshotai",))

    kept = snapshot["models"]["kimi-k2-retired"]
    assert (kept["input"], kept["output"]) == (0.6, 2.5), "the last-known rate must survive"
    assert "retained_since" in kept, "a retained rate must say it is one"
    assert "retained_since" not in snapshot["models"]["kimi-k3"]
    assert load_catalog(user_path=Path("/nonexistent")).get("kimi-k2-retired") is not None
    assert user_snapshot_path().exists()


def test_retention_cannot_mask_an_empty_response(burn_home, monkeypatch) -> None:
    """The floor must count what upstream returned, before anything is retained.

    Retaining first would let an empty response through by carrying the old file
    forward - quietly defeating the guard that exists because an empty response
    once unpriced every model on a real machine.
    """
    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    _fake_upstream(monkeypatch, plausible())
    refresh_snapshot()
    before = user_snapshot_path().read_bytes()

    _fake_upstream(monkeypatch, {"anthropic": {"models": {}}})
    with pytest.raises(ValueError, match="refusing to save"):
        refresh_snapshot()
    assert user_snapshot_path().read_bytes() == before


def test_a_retained_rate_keeps_its_original_date(burn_home, monkeypatch) -> None:
    """Refreshing again must not reset when a model was last seen upstream.

    Seeded with an old date rather than refreshed twice: two refreshes run on the
    same day, so a version that overwrote the date with today's would pass - a
    test that cannot fail, which is how this one was first written.
    """

    from burnometer.pricing.catalog import refresh_snapshot, user_snapshot_path

    seeded = plausible()["anthropic"]["models"]
    previous = {slug: {"vendor": "anthropic", "input": 1.0, "output": 1.0} for slug in seeded}
    previous["long-gone"] = {
        "vendor": "anthropic",
        "input": 1.0,
        "output": 2.0,
        "retained_since": "2026-01-01",
    }
    path = user_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": "2026-01-01T00:00:00+00:00", "models": previous}))

    _fake_upstream(monkeypatch, plausible())
    snapshot = refresh_snapshot()
    assert snapshot["models"]["long-gone"]["retained_since"] == "2026-01-01"


def test_claude_cache_reads_use_a_published_multiplier() -> None:
    """Cache reads are most of a Claude Code bill, and nothing else checks them.

    A 97-99% cache-hit rate means the read rate dominates cost, yet the overlay
    check covers only writes. Anthropic now publishes three read multipliers -
    0.1x generally, 0.05x on Opus 5.5, 0.025x on Fable and Mythos 5.1 - so a rate
    that matches none of them is a data error, not a new policy. Checked against
    platform.claude.com/docs/en/about-claude/pricing on 2026-09-23.
    """
    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT

    published = (0.1, 0.05, 0.025)
    catalog = load_catalog(snapshot_path=_PACKAGED_SNAPSHOT, user_path=Path("/nonexistent"))
    checked = 0
    for slug, price in catalog.prices.items():
        if not slug.startswith("claude-") or not price.input or price.cache_read is None:
            continue
        ratio = price.cache_read / price.input
        assert any(abs(ratio - m) < 1e-9 for m in published), (
            f"{slug}: cache read {price.cache_read} is {ratio:.4f}x input {price.input}, "
            f"which is none of Anthropic's published multipliers {published}"
        )
        checked += 1
    assert checked >= 10, f"expected to check most Claude models, checked {checked}"


def test_opus_5_5_is_priced_exactly_as_anthropic_publishes() -> None:
    """The model this was fixed for, pinned to the figures verified on 2026-09-23.

    It arrived unpriced - five turns on its first day showed as an em dash - and
    its 0.05x cache-read rate looked enough like a data error that it was checked
    against Anthropic's own page before being trusted.
    """
    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT

    p = load_catalog(snapshot_path=_PACKAGED_SNAPSHOT, user_path=Path("/nonexistent")).get(
        "claude-opus-5-5"
    )
    assert p is not None, "claude-opus-5-5 has no rate in the shipped snapshot"
    assert (p.input, p.cache_write_5m, p.cache_write_1h, p.cache_read, p.output) == (
        4.0,
        5.0,
        8.0,
        0.2,
        20.0,
    )


@pytest.mark.parametrize(
    ("generated_at", "wins"),
    [
        ("2020-01-01T00:00:00+00:00", "packaged"),
        ("2099-01-01T00:00:00+00:00", "refreshed"),
        ("not a timestamp", "refreshed"),
    ],
    ids=["older-refresh-loses", "newer-refresh-wins", "unreadable-date-keeps-old-rule"],
)
def test_the_newer_snapshot_wins(burn_home, generated_at: str, wins: str) -> None:
    """An upgrade shipping fresher rates has to reach people who once refreshed.

    A usable refreshed file used to win simply by existing, so a release adding
    claude-opus-5-5 would have changed nothing on any machine that had run
    `pricing refresh`: its older file, lacking the model, kept shadowing the new
    one. Found on the machine this was being fixed on.
    """

    from burnometer.pricing.catalog import (
        _PACKAGED_SNAPSHOT,
        active_snapshot_path,
        user_snapshot_path,
    )

    models = {f"m{i}": {"vendor": "anthropic", "input": 1.0, "output": 1.0} for i in range(60)}
    path = user_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": generated_at, "models": models}))

    expected = _PACKAGED_SNAPSHOT if wins == "packaged" else path
    assert active_snapshot_path() == expected


def test_a_refresh_never_prices_less_than_the_install_it_replaces(burn_home, monkeypatch) -> None:
    """A user's refresh must be a superset of the packaged snapshot.

    Retention first drew only on the file being replaced, so a user whose earlier
    refresh predated a model the packaged snapshot kept ended up without it - and
    because the newer snapshot wins, that refresh priced less than the install did.
    """

    from burnometer.pricing.catalog import _PACKAGED_SNAPSHOT, refresh_snapshot

    shipped = set(json.loads(_PACKAGED_SNAPSHOT.read_text())["models"])
    _fake_upstream(monkeypatch, plausible())  # a response carrying none of them
    refreshed = set(refresh_snapshot(vendors=None)["models"])

    missing = shipped - refreshed
    assert not missing, f"refreshing lost {len(missing)} packaged model(s): {sorted(missing)[:5]}"
