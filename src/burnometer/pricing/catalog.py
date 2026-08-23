"""The price catalog and its resolution chain.

Prices come from three layers, most specific first. Each layer may override
individual fields rather than a whole model, because the common case is
correcting exactly one number:

    ~/.burn-o-meter/pricing.toml   user overrides (enterprise or discount rates)
      -> overlay.toml              our corrections, shipped with the package
        -> snapshot.json           vendored models.dev data (MIT)
          -> UNPRICED              cost is NULL, never 0.0

The overlay exists because **no public pricing database records the 1-hour
cache-write rate.** models.dev publishes a single ``cache_write`` figure which
is the 5-minute rate (1.25x base input); the 1-hour rate is 2.0x. Claude Code
writes almost exclusively with 1-hour TTL, so a tool using the published number
understates cache-write cost by 37.5%. Every overlay entry therefore carries a
source URL and the date it was verified, so the correction is auditable rather
than folklore.

Network access lives here and nowhere else. ``tests/test_security.py`` asserts
that no other module in the package imports a networking library.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import burn_home
from ..safety import harden_path, secure_dir, secure_open_write

__all__ = [
    "Price",
    "Catalog",
    "load_catalog",
    "refresh_snapshot",
    "user_snapshot_path",
    "verify_overlay_multipliers",
    "active_snapshot_path",
    "MODELS_DEV_URL",
]

MODELS_DEV_URL = "https://models.dev/api.json"

#: The snapshot that ships inside the wheel. Read-only in practice: a pipx or uv
#: install lives somewhere the user cannot write, so a refresh must not target it.
_PACKAGED_SNAPSHOT = Path(__file__).with_name("snapshot.json")
_OVERLAY = Path(__file__).with_name("overlay.toml")


def user_snapshot_path() -> Path:
    """Where a refreshed snapshot is written.

    Under the user's data directory rather than into the installed package.
    ``pipx`` and ``uv tool install`` place packages in locations the user cannot
    write to, so refreshing in place would fail for exactly the installation
    methods the README recommends.
    """
    return burn_home() / "pricing-snapshot.json"


def active_snapshot_path() -> Path:
    """The snapshot in force: a refreshed one if present, else the packaged one."""
    refreshed = user_snapshot_path()
    return refreshed if refreshed.exists() else _PACKAGED_SNAPSHOT


#: Providers kept when vendoring the snapshot.
#:
#: Wider than the two agents we have adapters for, on purpose: Claude Code and
#: Codex can both be pointed at another provider, so a transcript can name a
#: model we have no adapter for. Without its rate here that usage lands in the
#: unpriced bucket and shows as "—" — correct, but useless.
#:
#: openrouter is deliberately excluded despite being the largest catalogue: its
#: 359 entries are mostly re-listings of models already here under a different
#: prefix, and it would roughly double the file for very little new coverage.
DEFAULT_VENDORS = (
    # agents we have adapters for
    "anthropic",
    "openai",
    "google",
    # open-weight model vendors, reachable via their own APIs
    "deepseek",
    "moonshotai",  # Kimi
    "kimi-for-coding",  # Kimi k3 coding plans
    "zhipuai",  # GLM
    "minimax",
    "alibaba",  # Qwen and others
    "mistral",
    "xai",
    # inference hosts that re-serve the above
    "groq",
    "togetherai",
    "fireworks-ai",
    "cerebras",
    # local runtimes — listed so their model names resolve; most carry no price,
    # which is the correct answer for self-hosted inference
    "lmstudio",
    "ollama-cloud",
)

# Cache-write multipliers relative to base input price, as published by
# Anthropic. These are not decoration: `overlay.toml` encodes the *product* of
# a model's input rate and the 1-hour multiplier, and
# `verify_overlay_multipliers()` checks every entry against these values. A
# hand-edited overlay that drifts from the published ratio is caught by a test
# rather than quietly mispricing every cache write.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class Price:
    """Rates in USD per million tokens.

    ``cache_write_1h`` is deliberately optional. When it is ``None`` the
    calculator falls back to the 5-minute rate and says so in the provenance,
    rather than silently guessing at a premium that may not have been charged.
    """

    input: float
    output: float
    cache_read: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float | None = None

    #: Long-context tier: above this many input tokens, ``tier`` rates apply.
    tier_threshold: int | None = None
    tier: Price | None = None

    context_limit: int | None = None
    vendor: str | None = None
    source: str = "unknown"

    def with_source(self, source: str) -> Price:
        return replace(self, source=source)


@dataclass(slots=True)
class Catalog:
    """Resolved prices, keyed by raw provider model slug."""

    prices: dict[str, Price]
    layers: list[str]
    generated_at: str | None = None

    def get(self, model: str) -> Price | None:
        """Exact match only.

        Fuzzy matching is deliberately absent: guessing that an unknown
        ``claude-opus-6`` should be priced like ``claude-opus-5`` would invent a
        number. An unknown model is reported as unpriced instead.
        """
        return self.prices.get(model.strip().lower()) if model else None

    def __contains__(self, model: str) -> bool:
        return self.get(model) is not None

    def __len__(self) -> int:
        return len(self.prices)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _price_from_mapping(raw: dict[str, Any], *, vendor: str | None, source: str) -> Price | None:
    if "input" not in raw or "output" not in raw:
        return None
    tier = None
    threshold = raw.get("tier_threshold")
    tier_raw = raw.get("tier")
    if isinstance(tier_raw, dict) and "input" in tier_raw:
        tier = Price(
            input=float(tier_raw["input"]),
            output=float(tier_raw["output"]),
            cache_read=float(tier_raw.get("cache_read", 0.0)),
            cache_write_5m=float(tier_raw.get("cache_write_5m", 0.0)),
            cache_write_1h=(
                float(tier_raw["cache_write_1h"]) if tier_raw.get("cache_write_1h") else None
            ),
            vendor=vendor,
            source=source,
        )
    return Price(
        input=float(raw["input"]),
        output=float(raw["output"]),
        cache_read=float(raw.get("cache_read", 0.0)),
        cache_write_5m=float(raw.get("cache_write_5m", 0.0)),
        cache_write_1h=float(raw["cache_write_1h"]) if raw.get("cache_write_1h") else None,
        tier_threshold=int(threshold) if threshold else None,
        tier=tier,
        context_limit=raw.get("context_limit"),
        vendor=vendor or raw.get("vendor"),
        source=source,
    )


def _merge(base: Price, patch: dict[str, Any], source: str) -> Price:
    """Apply a partial override onto an existing price.

    Field-level rather than model-level, because the overlay's whole job is
    usually to add one missing number to an otherwise-correct entry.
    """
    changed = [
        k
        for k in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
        if k in patch
    ]
    merged = replace(
        base,
        input=float(patch.get("input", base.input)),
        output=float(patch.get("output", base.output)),
        cache_read=float(patch.get("cache_read", base.cache_read)),
        cache_write_5m=float(patch.get("cache_write_5m", base.cache_write_5m)),
        cache_write_1h=(
            float(patch["cache_write_1h"]) if "cache_write_1h" in patch else base.cache_write_1h
        ),
    )
    detail = f"+{source}({','.join(changed)})" if changed else ""
    return merged.with_source(f"{base.source}{detail}")


def load_catalog(
    *,
    snapshot_path: Path | None = None,
    overlay_path: Path | None = None,
    user_path: Path | None = None,
) -> Catalog:
    """Build the catalog by layering snapshot, overlay and user overrides."""
    snapshot_path = snapshot_path or active_snapshot_path()
    overlay_path = overlay_path or _OVERLAY
    user_path = user_path if user_path is not None else burn_home() / "pricing.toml"

    prices: dict[str, Price] = {}
    layers: list[str] = []
    generated_at: str | None = None

    # Layer 1: vendored models.dev snapshot.
    if snapshot_path.exists():
        data = json.loads(snapshot_path.read_text())
        generated_at = data.get("generated_at")
        label = f"models.dev@{(generated_at or '')[:10]}"
        for slug, raw in (data.get("models") or {}).items():
            p = _price_from_mapping(raw, vendor=raw.get("vendor"), source=label)
            if p:
                prices[slug.lower()] = p
        layers.append(f"{label} ({len(prices)} models)")

    # Layer 2: our corrections.
    if overlay_path.exists():
        with open(overlay_path, "rb") as fh:
            overlay = tomllib.load(fh)
        count = 0
        for slug, patch in (overlay.get("models") or {}).items():
            key = slug.lower()
            src = f"overlay@{patch.get('verified', 'undated')}"
            if key in prices:
                prices[key] = _merge(prices[key], patch, src)
            else:
                p = _price_from_mapping(patch, vendor=patch.get("vendor"), source=src)
                if p:
                    prices[key] = p
            count += 1
        layers.append(f"overlay.toml ({count} corrections)")

    # Layer 3: user overrides.
    if user_path and user_path.exists():
        with open(user_path, "rb") as fh:
            user = tomllib.load(fh)
        count = 0
        for slug, patch in (user.get("models") or {}).items():
            key = slug.lower()
            if key in prices:
                prices[key] = _merge(prices[key], patch, "user")
            else:
                p = _price_from_mapping(patch, vendor=patch.get("vendor"), source="user")
                if p:
                    prices[key] = p
            count += 1
        layers.append(f"pricing.toml ({count} user overrides)")

    return Catalog(prices=prices, layers=layers, generated_at=generated_at)


def verify_overlay_multipliers(catalog: Catalog | None = None) -> list[str]:
    """Check every overlaid cache-write rate against the published multipliers.

    The overlay's whole reason for existing is that no public database records
    the 1-hour cache-write rate, so nothing upstream would catch a typo in it.
    This does: each entry must equal the model's input rate times the published
    multiplier. Returns a list of human-readable discrepancies, empty when
    consistent.
    """
    cat = catalog if catalog is not None else load_catalog(user_path=Path("/nonexistent"))
    problems: list[str] = []
    for slug, price in sorted(cat.prices.items()):
        if "overlay" not in price.source or price.cache_write_1h is None:
            continue
        expected = price.input * CACHE_WRITE_1H_MULTIPLIER
        if abs(price.cache_write_1h - expected) > 1e-6:
            problems.append(
                f"{slug}: cache_write_1h is {price.cache_write_1h}, expected "
                f"{expected} ({CACHE_WRITE_1H_MULTIPLIER}x input {price.input})"
            )
        if price.cache_write_5m:
            expected_5m = price.input * CACHE_WRITE_5M_MULTIPLIER
            if abs(price.cache_write_5m - expected_5m) > 1e-6:
                problems.append(
                    f"{slug}: cache_write_5m is {price.cache_write_5m}, expected "
                    f"{expected_5m} ({CACHE_WRITE_5M_MULTIPLIER}x input {price.input})"
                )
    return problems


# --------------------------------------------------------------------------
# Refresh — the single network egress in this package
# --------------------------------------------------------------------------


def refresh_snapshot(
    dest: Path | None = None,
    *,
    url: str = MODELS_DEV_URL,
    vendors: tuple[str, ...] | None = DEFAULT_VENDORS,
    timeout: int = 30,
) -> dict[str, Any]:
    """Fetch models.dev and rewrite the vendored snapshot.

    This is the only function in burn-o-meter that opens a socket, and it runs
    only when the user asks. It sends no identifiers, no cookies and no body —
    a plain GET.

    The response is normalised into our own flat schema rather than stored
    verbatim, so an upstream format change breaks this one function instead of
    silently mispricing everything downstream.
    """
    import urllib.request  # imported lazily: no socket code loads unless asked

    req = urllib.request.Request(url, headers={"User-Agent": "burn-o-meter"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https literal
        payload = json.loads(resp.read().decode("utf-8"))

    models: dict[str, Any] = {}
    for vendor, vdata in payload.items():
        if vendors and vendor not in vendors:
            continue
        for slug, m in (vdata.get("models") or {}).items():
            cost = m.get("cost") or {}
            if "input" not in cost or "output" not in cost:
                continue  # never invent a price for a model that has none
            entry: dict[str, Any] = {
                "vendor": vendor,
                "input": cost["input"],
                "output": cost["output"],
            }
            if "cache_read" in cost:
                entry["cache_read"] = cost["cache_read"]
            if "cache_write" in cost:
                # models.dev publishes exactly one cache-write number and it is
                # the 5-minute rate. The 1-hour rate is supplied by overlay.toml.
                entry["cache_write_5m"] = cost["cache_write"]
            over = cost.get("context_over_200k")
            tiers = cost.get("tiers") or []
            threshold = None
            if tiers and isinstance(tiers[0], dict):
                threshold = (tiers[0].get("tier") or {}).get("size")
            if isinstance(over, dict) and "input" in over:
                entry["tier_threshold"] = threshold or 200_000
                entry["tier"] = {
                    "input": over["input"],
                    "output": over["output"],
                    "cache_read": over.get("cache_read", 0.0),
                    "cache_write_5m": over.get("cache_write", 0.0),
                }
            limit = (m.get("limit") or {}).get("context")
            if limit:
                entry["context_limit"] = limit
            models[slug.lower()] = entry

    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": url,
        "source_license": "MIT",
        "source_repo": "https://github.com/anomalyco/models.dev",
        "note": (
            "Normalised from models.dev. cache_write_5m is that project's single "
            "'cache_write' field, which is the 5-minute rate. The 1-hour rate is "
            "not published anywhere upstream and lives in overlay.toml."
        ),
        "models": dict(sorted(models.items())),
    }

    dest = dest or user_snapshot_path()
    secure_dir(dest.parent)
    with secure_open_write(dest) as fh:
        fh.write((json.dumps(snapshot, indent=1, sort_keys=False) + "\n").encode("utf-8"))
    harden_path(dest)
    return snapshot
