"""Rendering for reports — tables and JSON.

Kept apart from ``cli.py`` so the JSON shape is defined once and every command
inherits the same honesty rules: bases never fuse, unpriced renders as a dash
rather than zero, and estimates carry their label.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from rich.table import Table

from .analytics import Block, BlockReport, Report, Row, Totals
from .models import CostBasis

__all__ = [
    "plural",
    "parse_since",
    "format_cost",
    "format_tokens",
    "report_table",
    "report_to_dict",
    "blocks_table",
    "BASIS_NOTE",
]

BASIS_NOTE = {
    CostBasis.API_BILLED: "billed per token against an API key",
    CostBasis.API_EQUIVALENT: "subscription — not billed per token; this is API-equivalent value",
    CostBasis.UNPRICED: "no published rate for these models",
    CostBasis.NOT_METERED: "not billed per token — self-hosted, or covered by a plan",
}

_RELATIVE = re.compile(r"^(\d+)\s*([dwhm])$", re.I)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str | None) -> datetime | None:
    """Accept ``7d`` / ``24h`` / ``2w`` / ``today`` / an ISO date."""
    if not value:
        return None
    text = value.strip().lower()
    now = datetime.now(UTC)
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if m := _RELATIVE.match(text):
        amount, unit = int(m.group(1)), m.group(2)
        if unit == "m":
            return now - timedelta(days=30 * amount)
        return now - timedelta(**{_UNITS[unit]: amount})
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"cannot read {value!r} as a time; try 7d, 24h, 2w, today, or 2026-08-01"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def plural(count: int, noun: str, suffix: str = "s") -> str:
    """Return ``count`` and ``noun``, agreeing in number: ``1 request``."""
    return f"{count:,} {noun}{'' if count == 1 else suffix}"


def format_cost(usd: float | None, basis: CostBasis | str | None = None) -> str:
    """Render money with its basis visible.

    An unpriced row shows an em dash, never ``$0.00``: "free" and "unknown" are
    different facts, and conflating them silently understates a bill.
    Subscription figures carry ``~`` because they are what the tokens would have
    cost, not money that changed hands.
    """
    if usd is None:
        return "[dim]—[/dim]"
    prefix = "~" if str(basis) == CostBasis.API_EQUIVALENT.value else ""
    return f"{prefix}${usd:,.2f}"


def format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _pct(value: float | None) -> str:
    return "[dim]—[/dim]" if value is None else f"{value * 100:.1f}%"


def report_table(report: Report, *, title: str, key_header: str) -> Table:
    t = Table(title=title, title_justify="left", header_style="bold")
    t.add_column(key_header)
    t.add_column("reqs", justify="right")
    t.add_column("cost", justify="right")
    t.add_column("share", justify="right")
    t.add_column("tokens", justify="right")
    t.add_column("cache hit", justify="right")
    t.add_column("eff $/Mtok", justify="right")

    for row in report.rows:
        tot = row.totals
        eff = tot.effective_rate
        t.add_row(
            row.key if not row.is_unpriced else f"{row.key} [yellow](unpriced)[/yellow]",
            f"{tot.requests:,}",
            format_cost(tot.cost_usd, row.basis),
            _pct(report.share_of_basis(row)),
            format_tokens(tot.tokens.total),
            _pct(tot.cache_hit_rate),
            "[dim]—[/dim]" if eff is None else f"{eff:,.2f}",
        )
    return t


def _totals_dict(t: Totals) -> dict[str, Any]:
    return {
        "requests": t.requests,
        "cost_usd": t.cost_usd,
        "unpriced_requests": t.unpriced_requests,
        "cache_hit_rate": t.cache_hit_rate,
        "effective_rate_usd_per_mtok": t.effective_rate,
        "tokens": {
            "input": t.tokens.input,
            "output": t.tokens.output,
            "reasoning": t.tokens.reasoning,
            "cache_read": t.tokens.cache_read,
            "cache_write_5m": t.tokens.cache_write_5m,
            "cache_write_1h": t.tokens.cache_write_1h,
            "total": t.tokens.total,
        },
    }


def _row_dict(report: Report, row: Row) -> dict[str, Any]:
    return {
        "key": row.key,
        "provider": row.provider,
        "cost_basis": row.basis.value,
        "price_source": row.price_source,
        "share_of_basis": report.share_of_basis(row),
        **_totals_dict(row.totals),
    }


def report_to_dict(report: Report) -> dict[str, Any]:
    """Machine-readable form.

    ``subtotals`` is a mapping keyed by cost basis and there is no combined
    total, so a downstream consumer inherits the no-fusing rule rather than
    having to know about it.
    """
    return {
        "dimension": report.dimension,
        "since": report.since.isoformat() if report.since else None,
        "until": report.until.isoformat() if report.until else None,
        "rows": [_row_dict(report, r) for r in report.rows],
        "subtotals": {b.value: _totals_dict(t) for b, t in report.subtotals.items()},
        "cost_basis_notes": {b.value: BASIS_NOTE[b] for b in report.subtotals if b in BASIS_NOTE},
        "unpriced_models": sorted(report.unpriced_models),
    }


def blocks_table(report: BlockReport, *, limit: int = 10) -> Table:
    t = Table(
        title=f"{report.window_hours}-hour usage windows",
        title_justify="left",
        header_style="bold",
    )
    t.add_column("window start")
    t.add_column("reqs", justify="right")
    t.add_column("tokens", justify="right")
    t.add_column("cost", justify="right")
    t.add_column("vs your median", justify="right")
    t.add_column("state")

    for block in report.blocks[-limit:]:
        ratio = report.relative_to_history(block)
        if block.is_active():
            remaining = block.remaining()
            mins = int(remaining.total_seconds() // 60)
            state = f"[green]active[/green] · ~{mins // 60}h{mins % 60:02d}m left"
        else:
            state = "[dim]closed[/dim]"
        t.add_row(
            block.start.astimezone().strftime("%m-%d %H:%M"),
            f"{block.requests:,}",
            format_tokens(block.tokens.total),
            format_cost(block.cost_usd, block.basis),
            "[dim]—[/dim]" if ratio is None else f"{ratio:.1f}x",
            state,
        )
    return t


def block_to_dict(block: Block) -> dict[str, Any]:
    return {
        "start": block.start.isoformat(),
        "end": block.end.isoformat(),
        "expires_at": block.expires_at.isoformat(),
        "active": block.is_active(),
        "remaining_seconds": int(block.remaining().total_seconds()),
        "requests": block.requests,
        "cost_usd": block.cost_usd,
        "cost_basis": block.basis.value,
        "tokens": {
            "input": block.tokens.input,
            "output": block.tokens.output,
            "cache_read": block.tokens.cache_read,
            "cache_write_5m": block.tokens.cache_write_5m,
            "cache_write_1h": block.tokens.cache_write_1h,
            "total": block.tokens.total,
        },
    }
