"""User configuration, loaded from ``~/.burn-o-meter/config.toml``.

Defaults are chosen so the tool is correct and private with no config file at
all. In particular ``project_paths`` defaults to ``basename``, so a fresh
install never records a full filesystem path — which would disclose the user's
account name and often their employer or client.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .safety import PROJECT_PATH_MODES, redact_path, secure_dir

__all__ = [
    "Config",
    "PrivacyConfig",
    "BillingConfig",
    "RetentionConfig",
    "burn_home",
    "default_db_path",
    "load_config",
]

_ENV_HOME = "BURNOMETER_HOME"


def burn_home() -> Path:
    """Return the data directory, honouring ``BURNOMETER_HOME`` (used by tests)."""
    override = os.environ.get(_ENV_HOME)
    return Path(override).expanduser() if override else Path.home() / ".burn-o-meter"


def default_db_path() -> Path:
    return burn_home() / "burn.db"


def default_config_path() -> Path:
    return burn_home() / "config.toml"


@dataclass(slots=True)
class PrivacyConfig:
    project_paths: str = "basename"

    def __post_init__(self) -> None:
        if self.project_paths not in PROJECT_PATH_MODES:
            raise ValueError(
                f"privacy.project_paths must be one of {PROJECT_PATH_MODES}, "
                f"got {self.project_paths!r}"
            )


BILLING_MODES = ("auto", "subscription", "api")


@dataclass(slots=True)
class BillingConfig:
    """How to interpret each provider's dollar figures.

    ``auto`` means "use whatever the logs reveal, else assume subscription".
    That default is deliberate: labelling usage ``api_equivalent`` states what
    the tokens *would* have cost, which is true either way, whereas
    ``api_billed`` asserts money actually left the user's account. When we
    cannot tell, only the first claim is safe to make.

    Set ``api`` if you pay per token against an API key and want the totals to
    read as real spend.
    """

    claude_code: str = "auto"
    codex: str = "auto"

    def __post_init__(self) -> None:
        for field_name in ("claude_code", "codex"):
            value = getattr(self, field_name)
            if value not in BILLING_MODES:
                raise ValueError(
                    f"billing.{field_name} must be one of {BILLING_MODES}, got {value!r}"
                )

    def subscription_for(self, provider: str, *, detected: bool | None = None) -> bool | None:
        """Resolve to ``True``/``False``/``None`` for the pricing calculator.

        ``detected`` is a provider-supplied signal (Codex records ``plan_type``
        in its logs, which proves a subscription). An explicit config setting
        always wins over detection, so a user can correct us.
        """
        mode = getattr(self, provider, "auto")
        if mode == "api":
            return False
        if mode == "subscription":
            return True
        return detected


@dataclass(slots=True)
class RetentionConfig:
    """How long to keep derived history.

    Quota readings are sampled continuously — Claude's desktop app writes one
    roughly every 15 minutes, and Codex emits one per turn — so they accumulate
    far faster than usage events and are only interesting recently. Usage events
    are the actual record of what was spent and default to being kept forever;
    everything is rebuildable from the provider logs anyway, so nothing here
    destroys information the tool cannot recover.
    """

    quota_days: int = 90
    #: 0 means keep indefinitely.
    events_days: int = 0

    def __post_init__(self) -> None:
        for field_name in ("quota_days", "events_days"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"retention.{field_name} must be a non-negative integer")


@dataclass(slots=True)
class Config:
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    billing: BillingConfig = field(default_factory=BillingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    source_path: Path | None = None

    @property
    def source_label(self) -> str:
        return redact_path(self.source_path) if self.source_path else "<defaults>"


def load_config(path: Path | None = None) -> Config:
    """Load configuration, falling back to defaults when no file exists.

    A malformed config is an error rather than a silent fallback: quietly
    reverting to defaults could turn a stricter ``project_paths`` setting back
    into a looser one without the user noticing.
    """
    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        return Config()

    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)

    privacy_raw = raw.get("privacy") or {}
    privacy = PrivacyConfig(
        project_paths=privacy_raw.get("project_paths", PrivacyConfig.project_paths)
    )
    billing_raw = raw.get("billing") or {}
    billing = BillingConfig(
        claude_code=billing_raw.get("claude_code", BillingConfig.claude_code),
        codex=billing_raw.get("codex", BillingConfig.codex),
    )
    retention_raw = raw.get("retention") or {}
    retention = RetentionConfig(
        quota_days=retention_raw.get("quota_days", RetentionConfig.quota_days),
        events_days=retention_raw.get("events_days", RetentionConfig.events_days),
    )
    return Config(privacy=privacy, billing=billing, retention=retention, source_path=cfg_path)


def ensure_home() -> Path:
    """Create the data directory with ``0700`` permissions and return it."""
    return secure_dir(burn_home())
