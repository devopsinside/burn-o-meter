"""Turning token counts into dollars, and being honest about what they mean.

Two rules govern everything here:

**Never invent a number.** An unknown model yields ``cost_usd=None`` and
``CostBasis.UNPRICED``. It is never priced as ``0.0``, because "free" and
"we don't know" are different facts and conflating them silently understates a
bill. Unpriced events still store their full token breakdown and still appear in
reports, marked ``—``.

**Never assert money that was not spent.** A user on a Claude Max or ChatGPT
plan is not billed per token. Reporting "$172 spent" would be fabrication, so
subscription usage is labelled ``API_EQUIVALENT`` — what the same tokens would
have cost at list rates — and every surface renders it with a leading ``~`` and
a note. The default for an undetectable provider is subscription, because that
claim is counterfactual and therefore always true, whereas ``API_BILLED`` would
assert a charge that may never have happened.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import CostBasis, TokenCounts, UsageEvent
from .catalog import Catalog, Price

__all__ = [
    "compute_cost",
    "is_not_metered",
    "is_local_provider",
    "LOCAL_PROVIDERS",
    "price_event",
    "price_events",
    "resolve_basis",
]

_PER_MILLION = 1_000_000.0


def _effective_price(price: Price, tokens: TokenCounts) -> tuple[Price, bool]:
    """Pick standard or long-context rates for this request.

    OpenAI charges roughly double above a context threshold (272k for the GPT-5
    family). The threshold applies to the size of the request's input, so it is
    measured against everything on the input side — fresh tokens, cache reads
    and cache writes alike — not against the output.
    """
    if price.tier is None or price.tier_threshold is None:
        return price, False
    input_side = tokens.input + tokens.cache_read + tokens.cache_write
    if input_side > price.tier_threshold:
        return price.tier, True
    return price, False


def compute_cost(tokens: TokenCounts, price: Price) -> tuple[float, str]:
    """Return ``(usd, provenance_note)`` for one request.

    The cache-write split is the point of this function. A 5-minute cache write
    bills at 1.25x base input; a 1-hour write bills at 2.0x. Every public
    pricing database publishes only the former, so anything that applies one
    blended rate understates 1-hour writes by 37.5% — and Claude Code writes
    almost exclusively with 1-hour TTL.
    """
    rates, tiered = _effective_price(price, tokens)
    notes: list[str] = []
    if tiered:
        notes.append("long-context tier")

    cache_write_1h_rate = rates.cache_write_1h
    if cache_write_1h_rate is None and tokens.cache_write_1h:
        # No 1-hour rate known for this model. Fall back to the 5-minute rate
        # and say so, rather than assuming a 2x premium that may not apply.
        cache_write_1h_rate = rates.cache_write_5m
        notes.append("1h cache-write rate unknown, used 5m rate")

    usd = (
        tokens.input * rates.input
        + tokens.output * rates.output
        + tokens.cache_read * rates.cache_read
        + tokens.cache_write_5m * rates.cache_write_5m
        + tokens.cache_write_1h * (cache_write_1h_rate or 0.0)
    ) / _PER_MILLION

    note = f"{price.source}" + (f" [{'; '.join(notes)}]" if notes else "")
    return usd, note


#: Providers that serve tokens from hardware the user already owns. There is no
#: per-token rate to look up, and never will be, so a missing catalog entry means
#: *not metered* rather than *unknown* - a distinction this project makes
#: everywhere else and would otherwise lose exactly here.
LOCAL_PROVIDERS = frozenset(
    {"ollama", "lmstudio", "lm-studio", "llamacpp", "llama-cpp", "llama.cpp", "mlx", "local"}
)


def is_local_provider(upstream: str | None) -> bool:
    """True when the tokens were served by something running on this machine."""
    return bool(upstream) and upstream.strip().lower() in LOCAL_PROVIDERS


def is_not_metered(price: Price) -> bool:
    """True when a model publishes no per-token rate.

    Both input and output at zero is the signal. No provider genuinely charges
    nothing per token for a production model; a zero rate means the tokens are
    covered some other way — a coding plan, or inference the user hosts.
    """
    return price.input == 0 and price.output == 0


def resolve_basis(provider: str, *, subscription: bool | None) -> CostBasis:
    """Decide how a dollar figure should be labelled.

    ``subscription=None`` means undetectable, which resolves to
    ``API_EQUIVALENT`` — see the module docstring for why that is the safe
    default rather than the pessimistic one.
    """
    if subscription is False:
        return CostBasis.API_BILLED
    return CostBasis.API_EQUIVALENT


def price_event(
    event: UsageEvent,
    catalog: Catalog,
    *,
    subscription: bool | None = None,
) -> UsageEvent:
    """Return a copy of ``event`` carrying a cost and its provenance."""
    # Checked before the catalog: a local model has no entry and never will, so
    # falling through to UNPRICED would report "we do not know the rate" when the
    # truth is "there is no rate". Both show no dollar figure, but only one of them
    # is honest, and only one tells the user their own hardware is not a bill.
    if is_local_provider(event.upstream_provider):
        return event.priced(
            None,
            CostBasis.NOT_METERED,
            f"served locally by {event.upstream_provider} [no per-token rate exists]",
        )

    price = catalog.get(event.model)
    if price is None:
        return event.priced(None, CostBasis.UNPRICED, f"no price for {event.model!r}")

    if is_not_metered(price):
        # A published rate of zero is not a price of zero. Providers use it for
        # plan-included models, and local runtimes have no per-token rate at
        # all. Either way no amount of money is the right answer.
        return event.priced(
            None,
            CostBasis.NOT_METERED,
            f"{price.source} [no per-token rate; usage covered by a plan or self-hosted]",
        )

    usd, note = compute_cost(event.tokens, price)
    return event.priced(usd, resolve_basis(event.provider, subscription=subscription), note)


def price_events(
    events: Iterable[UsageEvent],
    catalog: Catalog,
    *,
    subscription: bool | None = None,
) -> list[UsageEvent]:
    return [price_event(e, catalog, subscription=subscription) for e in events]
