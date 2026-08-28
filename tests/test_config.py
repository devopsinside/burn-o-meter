"""Configuration loading, with an emphasis on *partial* config files.

Every one of these was a real defect. ``load_config`` read its fallbacks off the
dataclasses themselves (``PrivacyConfig.project_paths``), but those are
``slots=True`` dataclasses, so the class attribute is a slot descriptor rather
than the default value — and any config file that omitted a key raised
``ValueError`` on that descriptor. A hand-written config setting one option is
the normal case, so the only file that loaded was one that set everything.

``BillingConfig`` separately hardcoded ``claude_code`` and ``codex``, so
``billing.opencode = "api"`` parsed, validated, and was then discarded — while
``doctor`` printed advice telling users to set exactly that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from burnometer.adapters import get_adapters
from burnometer.config import (
    BILLING_MODES,
    BillingConfig,
    PrivacyConfig,
    RetentionConfig,
    load_config,
)


def write_config(home: Path, body: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body)
    return path


def test_no_config_file_uses_defaults(burn_home: Path) -> None:
    cfg = load_config()
    assert cfg.privacy.project_paths == "basename"
    assert cfg.source_label == "<defaults>"


@pytest.mark.parametrize(
    "body",
    [
        "",
        '[privacy]\nproject_paths = "hash"\n',
        '[billing]\ncodex = "api"\n',
        "[retention]\nquota_days = 45\n",
        '[privacy]\nproject_paths = "none"\n\n[billing]\nclaude_code = "subscription"\n',
    ],
    ids=["empty", "privacy-only", "billing-only", "retention-only", "two-sections"],
)
def test_partial_config_loads(burn_home: Path, body: str) -> None:
    """A file that sets some keys and omits others must load, not raise."""
    write_config(burn_home, body)
    load_config()


def test_omitted_keys_keep_their_defaults(burn_home: Path) -> None:
    write_config(burn_home, "[retention]\nquota_days = 45\n")
    cfg = load_config()
    assert cfg.retention.quota_days == 45
    assert cfg.retention.events_days == RetentionConfig().events_days
    assert cfg.privacy.project_paths == PrivacyConfig().project_paths
    assert cfg.billing.claude_code == BillingConfig().claude_code


def test_every_provider_honours_its_billing_setting(burn_home: Path) -> None:
    """Not just the two the dataclass happens to name.

    ``doctor`` advises setting ``billing.<provider> = "api"`` for any provider it
    priced as API-equivalent, so every registered provider must honour it.
    """
    providers = [a.name for a in get_adapters()]
    assert "opencode" in providers, "guard: expected more than the two original providers"

    body = "[billing]\n" + "".join(f'{p} = "api"\n' for p in providers)
    write_config(burn_home, body)
    billing = load_config().billing

    for provider in providers:
        assert billing.subscription_for(provider) is False, (
            f"billing.{provider} = 'api' was ignored"
        )


def test_billing_defaults_to_auto_for_unknown_provider(burn_home: Path) -> None:
    write_config(burn_home, "")
    assert load_config().billing.subscription_for("some-future-agent") is None


@pytest.mark.parametrize("mode", BILLING_MODES)
def test_billing_accepts_every_documented_mode(burn_home: Path, mode: str) -> None:
    write_config(burn_home, f'[billing]\nopencode = "{mode}"\n')
    load_config()


def test_invalid_values_are_rejected_not_silently_defaulted(burn_home: Path) -> None:
    """Falling back to a default could loosen a stricter privacy setting."""
    write_config(burn_home, '[privacy]\nproject_paths = "bogus"\n')
    with pytest.raises(ValueError, match="privacy.project_paths"):
        load_config()

    write_config(burn_home, '[billing]\nopencode = "bogus"\n')
    with pytest.raises(ValueError, match="billing.opencode"):
        load_config()


def test_plural_agrees_in_number() -> None:
    """``across 1 requests`` shipped in the models report subtotal."""
    from burnometer.report import plural

    assert plural(0, "request") == "0 requests"
    assert plural(1, "request") == "1 request"
    assert plural(2, "request") == "2 requests"
    assert plural(1234, "request") == "1,234 requests"


def test_the_documented_config_block_actually_loads(burn_home: Path) -> None:
    """docs/configuration.md shows a full config file; it has to be valid.

    It is the file users copy, and it sets every key — so it was the one shape
    that loaded while every partial one raised.
    """
    import re

    doc = Path(__file__).parent.parent / "docs" / "configuration.md"
    block = re.search(r"```toml\n(.*?)```", doc.read_text(), re.S)
    assert block, "docs/configuration.md no longer contains a toml block"

    write_config(burn_home, block.group(1))
    cfg = load_config()
    assert cfg.privacy.project_paths == PrivacyConfig().project_paths
    assert cfg.billing.mode_for("opencode") == "auto"
