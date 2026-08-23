"""Command line interface.

Uses stdlib ``argparse`` rather than click or typer: guarantee G7 keeps the
runtime dependency list to one entry, and subcommands are not hard.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import get_adapters
from .agent import (
    DEFAULT_INTERVAL,
    agent_status,
    describe,
    install_agent,
    uninstall_agent,
)
from .analytics import aggregate, blocks
from .config import burn_home, default_config_path, default_db_path, load_config
from .pricing import load_catalog
from .report import (
    BASIS_NOTE,
    block_to_dict,
    blocks_table,
    format_cost,
    parse_since,
    report_table,
    report_to_dict,
)
from .safety import CREDENTIAL_FILENAMES, CREDENTIAL_SUFFIXES, redact_path
from .scan import reprice, scan
from .snapshot import write_snapshot
from .store import Store

console = Console()


def _mode_of(p: Path) -> str:
    if not p.exists():
        return "-"
    return oct(stat.S_IMODE(p.stat().st_mode))


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what the tool can see, where its data lives, and how it is priced.

    This is the transparency surface: every number this tool prints elsewhere
    should be traceable to something shown here.
    """
    if args.security:
        return _doctor_security()
    if args.pricing:
        return _doctor_pricing()

    cfg = load_config()
    db_path = default_db_path()

    t = Table(title="burn-o-meter doctor", title_justify="left", show_header=False, box=None)
    t.add_column(style="dim", width=18)
    t.add_column()
    t.add_row("version", __version__)
    t.add_row("python", sys.version.split()[0])
    t.add_row("data dir", f"{redact_path(burn_home())}  [dim]({_mode_of(burn_home())})[/dim]")
    t.add_row("config", cfg.source_label)
    t.add_row("project paths", f"{cfg.privacy.project_paths}  [dim](privacy.project_paths)[/dim]")
    console.print(t)
    console.print()

    # Providers — discovery only; nothing is opened.
    pt = Table(title="Providers", title_justify="left", header_style="bold")
    pt.add_column("provider")
    pt.add_column("log location")
    pt.add_column("files", justify="right")
    pt.add_column("status")
    for adapter in get_adapters():
        for src in adapter.sources():
            found = list(src.discover()) if src.root.exists() else []
            if not src.root.exists():
                status = "[dim]not installed[/dim]"
            elif not getattr(adapter, "implemented", True):
                # Detected but unreadable. Saying "ready" here would imply this
                # provider's usage is included in the totals, which it is not.
                status = "[yellow]detected — parser not yet built[/yellow]"
            elif not found:
                status = "[yellow]no logs found[/yellow]"
            else:
                status = "[green]ready[/green]"
            location = f"{redact_path(src.root)}/{src.glob}"
            if src.env_var and not src.root.exists():
                # Say which knob to turn, rather than leaving "not installed" to
                # mean both "you don't use it" and "we looked in the wrong place".
                location += f"   [dim](or set ${src.env_var})[/dim]"
            pt.add_row(adapter.display_name, location, str(len(found)), status)
    console.print(pt)
    console.print()

    # Store
    if not db_path.exists():
        console.print(
            f"[yellow]no database yet[/yellow] at {redact_path(db_path)} — "
            "run [bold]burn-o-meter scan[/bold] to build one"
        )
        return 0

    with Store.open(db_path, read_only=True) as store:
        s = store.stats()
    st = Table(title="Store", title_justify="left", show_header=False, box=None)
    st.add_column(style="dim", width=18)
    st.add_column()
    st.add_row("database", f"{redact_path(db_path)}  [dim]({_mode_of(db_path)})[/dim]")
    st.add_row("schema", f"v{s['schema_version']}")
    st.add_row("events", f"{s['events']:,}")
    st.add_row("files tracked", str(s["files_tracked"]))
    st.add_row("quota readings", str(s["quota_readings"]))
    st.add_row("providers", ", ".join(s["providers"]) or "[dim]none[/dim]")
    st.add_row("models", ", ".join(s["models"]) or "[dim]none[/dim]")
    # How the dollar figures should be read, and how to change it. Getting this
    # wrong is invisible: an API-key user sees their real bill labelled as a
    # hypothetical, and nothing prompts them to look.
    from .scan import detect_subscription_in_store

    with Store.open(db_path, read_only=True) as store:
        bases = []
        for provider in s["providers"]:
            mode = getattr(cfg.billing, provider, "auto")
            detected = detect_subscription_in_store(store, provider)
            if mode == "api":
                how, basis = "set in config", "real spend"
            elif mode == "subscription":
                how, basis = "set in config", "API-equivalent"
            elif detected:
                how, basis = "detected", "API-equivalent"
            else:
                how, basis = "assumed — set [billing] if you pay per token", "API-equivalent"
            bases.append(f"{provider}: {basis} ({how})")
    st.add_row("billing", "\n".join(bases))

    if s["unpriced"]:
        st.add_row("unpriced events", f"[yellow]{s['unpriced']:,}[/yellow] (shown as '—')")
    if s["earliest"]:
        st.add_row("range", f"{s['earliest']} .. {s['latest']}")
    console.print(st)
    return 0


def _doctor_security() -> int:
    """Print the security posture so the claims can be audited, not trusted."""
    console.print("[bold]burn-o-meter security posture[/bold]\n")

    g = Table(show_header=True, header_style="bold", title="Guarantees", title_justify="left")
    g.add_column("id", width=4)
    g.add_column("guarantee")
    g.add_column("enforcement")
    for row in [
        ("G1", "Prompts and completions are never read", "allowlist extraction + CI canary scan"),
        (
            "G2",
            "Credential files are never opened",
            "deny-list + narrow globs + symlink containment",
        ),
        ("G3", "No network by default", "one opt-in egress; autouse socket blocker in tests"),
        ("G4", "Data is private on disk", "0700 dir, 0600 db, read-only UI connection"),
        ("G5", "Project paths are reducible", "privacy.project_paths, applied before storage"),
        ("G6", "Errors never carry content", "redact() + AdapterError(path, line)"),
        ("G7", "Minimal supply chain", "one runtime dependency (rich)"),
    ]:
        g.add_row(*row)
    console.print(g)
    console.print()

    e = Table(show_header=True, header_style="bold", title="Network egress", title_justify="left")
    e.add_column("when")
    e.add_column("destination")
    e.add_column("sends")
    e.add_row(
        "burn-o-meter pricing refresh [dim](manual only)[/dim]",
        "https://models.dev/api.json",
        "nothing — plain GET",
    )
    e.add_row(
        "menu bar → Check for Updates… [dim](manual only)[/dim]",
        "https://api.github.com/…/releases/latest",
        "nothing — unauthenticated GET, no cookies",
    )
    console.print(e)
    console.print(
        "[dim]Those two are the whole list, and neither runs on a timer, at launch, "
        "or in the background — only when you ask. No telemetry, analytics or crash "
        "reporting exists anywhere in this codebase.[/dim]\n"
    )

    f = Table(show_header=True, header_style="bold", title="File permissions", title_justify="left")
    f.add_column("path")
    f.add_column("mode", justify="right")
    f.add_column("expected", justify="right")
    for p, want in [
        (burn_home(), "0o700"),
        (default_db_path(), "0o600"),
        (Path(str(default_db_path()) + "-wal"), "0o600"),
        (default_config_path(), "0o600"),
    ]:
        actual = _mode_of(p)
        ok = actual in (want, "-")
        f.add_row(redact_path(p), f"[{'green' if ok else 'red'}]{actual}[/]", want)
    console.print(f)
    console.print()

    console.print(
        f"[dim]Credential deny-list: {len(CREDENTIAL_FILENAMES)} filenames, "
        f"{len(CREDENTIAL_SUFFIXES)} suffixes, plus any path under "
        f".ssh/.gnupg/.aws/.kube/.docker.[/dim]"
    )
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Read new log data into the store."""
    cfg = load_config()
    db_path = default_db_path()

    with Store.open(db_path) as store:
        report = scan(store, config=cfg, force=args.force)
        total = store.count_events()
        # Refresh the UI payload in the same pass, so the menu bar is never
        # older than the last scan.
        write_snapshot(store)

    if args.quiet:
        # Scheduled runs write to a log file. Stay silent unless something went
        # wrong, so the log stays readable instead of filling with "0 new".
        if report.files_failed or report.integrity_failures:
            console.print(
                f"[scan] {report.files_failed} file(s) failed, "
                f"{report.integrity_failures} integrity failure(s)"
            )
            for err in report.errors[:5]:
                console.print(f"[scan]   {err}")
            return 1
        if report.events_new:
            console.print(f"[scan] +{report.events_new} events ({total:,} total)")
        return 0

    t = Table(title="scan", title_justify="left", show_header=False, box=None)
    t.add_column(style="dim", width=18)
    t.add_column()
    t.add_row("files", f"{report.files_parsed} parsed, {report.files_unchanged} unchanged")
    t.add_row("lines", f"{report.lines_read:,} read, {report.lines_skipped:,} skipped")
    t.add_row("events", f"{report.events_new:,} new, {report.events_already_known:,} already known")
    if report.duplicates_dropped:
        t.add_row("duplicates", f"{report.duplicates_dropped:,} collapsed")
    if report.integrity_checks:
        passed = report.integrity_checks - report.integrity_failures
        style = "green" if not report.integrity_failures else "red"
        t.add_row(
            "integrity",
            f"[{style}]{passed}/{report.integrity_checks}[/{style}] sessions reconcile "
            f"with the provider's own totals",
        )
    if report.events_unpriced:
        models = ", ".join(sorted(report.unpriced_models)[:3])
        t.add_row("unpriced", f"[yellow]{report.events_unpriced:,}[/yellow] ({models})")
    if report.quotas_new:
        t.add_row("quota", f"{report.quotas_new:,} readings")
    t.add_row("total stored", f"{total:,}")
    t.add_row("took", f"{report.duration_s * 1000:.0f} ms")
    console.print(t)

    if report.files_failed:
        console.print(f"\n[yellow]{report.files_failed} file(s) failed[/yellow]")
        for err in report.errors[:5]:
            console.print(f"  [dim]{err}[/dim]")

    if args.stats and report.records_seen:
        pct = report.duplicates_dropped / report.records_seen * 100
        console.print(
            f"\n[dim]{report.records_seen:,} usage records on disk collapsed to "
            f"{report.events_found:,} unique ({pct:.1f}% were duplicates the provider "
            f"wrote more than once). Summing without deduplicating would overcount "
            f"by {report.records_seen / max(report.events_found, 1):.2f}x.[/dim]"
        )
    return 0


def _fmt_cost(usd: float | None, basis: str) -> str:
    """Render a cost with its basis made visible.

    An unpriced event shows an em dash, never $0.00 — "free" and "unknown" are
    different facts. Subscription usage carries a leading '~' because it is what
    the tokens would have cost, not money that changed hands.
    """
    if usd is None:
        return "[dim]—[/dim]"
    return f"~${usd:,.2f}" if basis == "api_equivalent" else f"${usd:,.2f}"


def cmd_reprice(args: argparse.Namespace) -> int:
    """Recompute stored costs against the current catalog."""
    db_path = default_db_path()
    if not db_path.exists():
        console.print("[yellow]no database yet[/yellow] — run [bold]burn-o-meter scan[/bold]")
        return 1
    with Store.open(db_path) as store:
        updated, unpriced = reprice(store)
    console.print(f"repriced [bold]{updated:,}[/bold] events")
    if unpriced:
        console.print(
            f"[yellow]{unpriced:,} still unpriced[/yellow] — shown as '—', never as $0.00"
        )
    return 0


def _doctor_pricing() -> int:
    """Show where every rate comes from, and how fresh it is."""
    catalog = load_catalog()

    console.print("[bold]pricing catalog[/bold]\n")
    for layer in catalog.layers:
        console.print(f"  [dim]•[/dim] {layer}")
    console.print()

    t = Table(header_style="bold", title="Rates in use", title_justify="left")
    t.add_column("model")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("cache read", justify="right")
    t.add_column("cw 5m", justify="right")
    t.add_column("cw 1h", justify="right")
    t.add_column("source")

    db_path = default_db_path()
    models: list[str] = []
    if db_path.exists():
        with Store.open(db_path, read_only=True) as store:
            models = store.stats()["models"]
    if not models:
        models = [m for m in catalog.prices if m.startswith(("claude-opus", "gpt-5.6"))][:6]

    for model in models:
        price = catalog.get(model)
        if price is None:
            t.add_row(model, "[dim]—[/dim]", "", "", "", "", "[yellow]unpriced[/yellow]")
            continue
        cw1h = (
            f"[green]{price.cache_write_1h:g}[/green]"
            if price.cache_write_1h is not None
            else "[yellow]unknown[/yellow]"
        )
        t.add_row(
            model,
            f"{price.input:g}",
            f"{price.output:g}",
            f"{price.cache_read:g}",
            f"{price.cache_write_5m:g}",
            cw1h,
            price.source,
        )
    console.print(t)
    console.print(
        "\n[dim]Rates are USD per million tokens. cw 5m / cw 1h are prompt-cache writes:\n"
        "Anthropic bills 5-minute TTL at 1.25x base input and 1-hour TTL at 2.0x. Public\n"
        "pricing databases publish only the 5-minute figure, so overlay.toml supplies the\n"
        "1-hour rate — without it, cache-write cost is understated by 37.5%.[/dim]"
    )
    return 0


_DIMENSION_HEADERS = {
    "model": "model",
    "family": "family",
    "project": "project",
    "provider": "provider",
    "day": "date",
    "session": "session",
    "effort": "effort",
}


def _open_store_or_warn():
    db_path = default_db_path()
    if not db_path.exists():
        console.print("[yellow]no data yet[/yellow] — run [bold]burn-o-meter scan[/bold] first")
        return None
    return Store.open(db_path, read_only=True)


def _print_subtotals(report) -> None:
    """Print one subtotal per cost basis, never a combined figure."""
    if not report.subtotals:
        return
    console.print()
    for basis, totals in report.subtotals.items():
        console.print(
            f"  [bold]{format_cost(totals.cost_usd, basis)}[/bold] "
            f"across {totals.requests:,} requests   [dim]{BASIS_NOTE.get(basis, '')}[/dim]"
        )
    if len(report.subtotals) > 1:
        console.print(
            "  [dim]Shown separately on purpose: adding a real charge to an "
            "API-equivalent figure would produce a number that means nothing.[/dim]"
        )
    if report.unpriced_models:
        console.print(
            f"  [yellow]{len(report.unpriced_models)} unpriced model(s)[/yellow]: "
            f"{', '.join(sorted(report.unpriced_models))} [dim]— shown as '—', never $0.00[/dim]"
        )


def _emit(report, args, *, title: str) -> int:
    if args.json:
        console.print_json(json.dumps(report_to_dict(report)))
        return 0
    if not report.rows:
        console.print("[dim]no usage in this period[/dim]")
        return 0
    header = _DIMENSION_HEADERS.get(report.dimension, report.dimension)
    console.print(report_table(report, title=title, key_header=header))
    _print_subtotals(report)
    return 0


def _aggregate_command(args, dimension: str, title: str) -> int:
    store = _open_store_or_warn()
    if store is None:
        return 1
    with store:
        report = aggregate(
            store,
            dimension,
            since=parse_since(getattr(args, "since", None)),
            until=parse_since(getattr(args, "until", None)),
            provider=getattr(args, "provider", None),
            limit=getattr(args, "limit", None),
        )
    return _emit(report, args, title=title)


def cmd_models(args: argparse.Namespace) -> int:
    return _aggregate_command(args, "model", "Usage by model")


def cmd_projects(args: argparse.Namespace) -> int:
    return _aggregate_command(args, "project", "Usage by project")


def cmd_sessions(args: argparse.Namespace) -> int:
    return _aggregate_command(args, "session", "Usage by session")


def cmd_daily(args: argparse.Namespace) -> int:
    return _aggregate_command(args, "day", "Usage by day")


def cmd_today(args: argparse.Namespace) -> int:
    """The default view: what today cost, and where the current window stands."""
    args.since, args.until, args.limit = "today", None, None
    store = _open_store_or_warn()
    if store is None:
        return 1

    with store:
        since = parse_since("today")
        by_model = aggregate(store, "model", since=since)
        block_report = blocks(store)
        quota_rows = list(store.latest_quota("codex"))

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "today": report_to_dict(by_model),
                    "current_window": (
                        block_to_dict(block_report.current) if block_report.current else None
                    ),
                    "quota": [
                        {
                            "provider": r["provider"],
                            "window": r["window_name"],
                            "used_percent": r["used_percent"],
                            "window_minutes": r["window_minutes"],
                            "resets_at": r["resets_at"],
                            "plan_type": r["plan_type"],
                            "source": r["source"],
                        }
                        for r in quota_rows
                    ],
                }
            )
        )
        return 0

    if by_model.rows:
        console.print(report_table(by_model, title="Today", key_header="model"))
        _print_subtotals(by_model)
    else:
        console.print("[dim]no usage recorded today[/dim]")

    current = block_report.current
    if current is not None:
        mins = int(current.remaining().total_seconds() // 60)
        ratio = block_report.relative_to_history(current)
        console.print()
        console.print(
            f"  [bold]current 5h window[/bold]  "
            f"{format_cost(current.cost_usd, current.basis)} over {current.requests:,} requests"
            f" · ~{mins // 60}h{mins % 60:02d}m left"
        )
        if ratio is not None:
            console.print(
                f"  [dim]{ratio:.1f}x your median window. Claude publishes no token "
                f"limit for subscription plans and stores no quota locally, so this "
                f"compares against your own history rather than inventing a "
                f"percentage.[/dim]"
            )

    if quota_rows:
        console.print()
        for r in quota_rows:
            days = (r["window_minutes"] or 0) / 1440
            console.print(
                f"  [bold]codex {r['window_name']}[/bold]  {r['used_percent']:.0f}% used"
                f" of a {days:.0f}-day window · plan {r['plan_type']}"
                f"   [green]exact[/green] [dim](reported by Codex itself)[/dim]"
            )
    return 0


def cmd_blocks(args: argparse.Namespace) -> int:
    store = _open_store_or_warn()
    if store is None:
        return 1
    with store:
        report = blocks(
            store,
            provider=args.provider or "claude_code",
            since=parse_since(getattr(args, "since", None)),
        )

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "window_hours": report.window_hours,
                    "blocks": [block_to_dict(b) for b in report.blocks],
                }
            )
        )
        return 0

    if not report.blocks:
        console.print("[dim]no usage windows in this period[/dim]")
        return 0
    console.print(blocks_table(report, limit=args.limit or 10))
    console.print(
        "\n[dim]'vs your median' compares a window against your own completed windows.\n"
        "There is deliberately no 'percent of limit': Anthropic publishes no token limit for\n"
        "subscription plans and Claude Code stores no quota on disk, so any percentage would\n"
        "be invented. Time left is derived from the first request in the window — Anthropic\n"
        "does not document the anchor, so treat it as accurate to within an hour.\n"
        "Codex quota is exact and shown by `burn-o-meter today`.[/dim]"
    )
    return 0


def cmd_pricing(args: argparse.Namespace) -> int:
    """Fetch current rates from models.dev.

    The only command in burn-o-meter that opens a socket, and only because it was
    typed. A snapshot ships with the package, so this is optional - it exists for
    when a model is newer than the release you installed.
    """
    from .pricing.catalog import MODELS_DEV_URL, refresh_snapshot, user_snapshot_path
    from .safety import redact

    dest = user_snapshot_path()
    console.print(f"[dim]fetching {MODELS_DEV_URL}[/dim]")
    try:
        data = refresh_snapshot(dest)
    except Exception as exc:  # noqa: BLE001 - the network fails in many ways
        console.print(f"[red]could not refresh:[/red] {redact(str(exc))}")
        console.print("[dim]the bundled snapshot is still in use; nothing changed[/dim]")
        return 1

    models = len(data.get("models", {}))
    if not models:
        # Writing a file and reporting nothing in it would be worse than failing.
        console.print("[red]refresh returned no models[/red] — the bundled snapshot is unchanged")
        return 1
    console.print(f"  [dim]models[/dim]   {models:,}")
    console.print(f"  [dim]written[/dim]  {dest}")
    console.print(
        "\n[dim]Run [/dim]burn-o-meter reprice[dim] to apply these to stored events.[/dim]"
    )
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """Manage the background scanner."""
    if args.agent_command == "status":
        for line in describe(agent_status()):
            console.print(f"  [dim]{line.split(maxsplit=1)[0]:9}[/dim]{line.split(maxsplit=1)[1]}")
        return 0

    if args.agent_command == "uninstall":
        removed = uninstall_agent()
        console.print(
            "background scanning removed" if removed else "[dim]nothing was installed[/dim]"
        )
        return 0

    # install
    try:
        path = install_agent(interval=args.interval, load=not args.no_load)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.print(f"installed [bold]{redact_path(path)}[/bold]")
    console.print(f"  scanning every {args.interval}s at low priority")
    if args.no_load:
        console.print(
            "  [dim]not loaded — run `burn-o-meter agent install` without "
            "--no-load, or `launchctl bootstrap` it yourself[/dim]"
        )
    else:
        status = agent_status()
        console.print(
            f"  [{'green' if status.loaded else 'yellow'}]"
            f"{'loaded and running' if status.loaded else 'written but not loaded'}[/]"
        )
    console.print("  [dim]remove with: burn-o-meter agent uninstall[/dim]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="burnometer",
        description="An honest meter for AI coding-agent usage, cost and quota.",
    )
    p.add_argument("--version", action="version", version=f"burn-o-meter {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="show what is detected, stored and how it is secured")
    d.add_argument(
        "--security",
        action="store_true",
        help="print the security posture: guarantees, egress points and file permissions",
    )
    d.add_argument(
        "--pricing",
        action="store_true",
        help="print the price catalog, its layers and the source of every rate",
    )
    d.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("scan", help="read new usage data from provider logs")
    sc.add_argument(
        "--force",
        action="store_true",
        help="re-read every file from the beginning, discarding saved offsets",
    )
    sc.add_argument("--stats", action="store_true", help="show deduplication detail")
    sc.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing unless something changed or failed (for scheduled runs)",
    )
    sc.set_defaults(func=cmd_scan)

    rp = sub.add_parser("reprice", help="recompute stored costs against current rates")
    rp.set_defaults(func=cmd_reprice)

    def _add_common(sp, *, with_provider: bool = True) -> None:
        sp.add_argument("--since", help="7d, 24h, 2w, today, or an ISO date")
        sp.add_argument("--until", help="upper bound (exclusive)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.add_argument("--limit", type=int, help="show at most N rows")
        if with_provider:
            sp.add_argument("--provider", help="claude_code or codex")

    tdy = sub.add_parser("today", help="what today cost, and the current usage window")
    tdy.add_argument("--json", action="store_true", help="machine-readable output")
    tdy.set_defaults(func=cmd_today)

    for name, fn, helptext in [
        ("models", cmd_models, "usage and cost per model"),
        ("projects", cmd_projects, "usage and cost per project"),
        ("sessions", cmd_sessions, "usage and cost per session"),
        ("daily", cmd_daily, "usage and cost per day"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        _add_common(sp)
        sp.set_defaults(func=fn)

    bl = sub.add_parser("blocks", help="rolling 5-hour usage windows")
    _add_common(bl)
    bl.set_defaults(func=cmd_blocks)

    pr = sub.add_parser("pricing", help="inspect or refresh model rates")
    pr_sub = pr.add_subparsers(dest="pricing_command", required=True)
    pr_sub.add_parser("refresh", help="fetch current rates from models.dev (the only network call)")
    pr.set_defaults(func=cmd_pricing)

    ag = sub.add_parser("agent", help="run scans automatically in the background")
    ag_sub = ag.add_subparsers(dest="agent_command", required=True)
    ag_install = ag_sub.add_parser("install", help="schedule background scanning")
    ag_install.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds between scans (default {DEFAULT_INTERVAL}, minimum 15)",
    )
    ag_install.add_argument(
        "--no-load", action="store_true", help="write the plist but do not start it"
    )
    ag_sub.add_parser("uninstall", help="stop and remove background scanning")
    ag_sub.add_parser("status", help="show whether background scanning is active")
    ag.set_defaults(func=cmd_agent)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
