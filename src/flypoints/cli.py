"""Typer CLI: a thin layer over ``flypoints.engine.search``."""
from __future__ import annotations

import sys
from datetime import date, datetime

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table

from . import config, engine
from .models import Bank, Cabin, RankedOption, SearchQuery
from .providers.base import AuthError

app = typer.Typer(help="Award-flight search for Capital One miles and Chase Ultimate Rewards.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_dates(text: str) -> tuple[date, date]:
    """"2027-03-01:2027-03-31" or a single "2027-03-05"."""
    try:
        start, _, end = text.partition(":")
        d_from = datetime.strptime(start, "%Y-%m-%d").date()
        d_to = datetime.strptime(end, "%Y-%m-%d").date() if end else d_from
    except ValueError:
        raise typer.BadParameter("use YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD") from None
    if d_to < d_from:
        raise typer.BadParameter("end date is before start date")
    return d_from, d_to


def _route(o: RankedOption) -> str:
    segs = o.offer.segments
    if not segs:
        return f"{o.offer.origin}-{o.offer.destination}"
    return "-".join([segs[0].origin] + [s.destination for s in segs])


def _other_bank_points(o: RankedOption) -> str:
    others = [f"{b.bank.label} {b.bank_points:,}" for b in o.bank_options if b.bank != o.best_bank]
    return ", ".join(others) or "-"


def _options_table(options: list[RankedOption]) -> Table:
    t = Table(show_lines=False)
    for col in ("Date", "Route", "Program", "Cabin"):
        t.add_column(col)
    for col in ("Miles", "Taxes", "Best bank pts"):
        t.add_column(col, justify="right")
    t.add_column("Best bank")
    t.add_column("Other bank")
    for col in ("Cash", "cpp", "Seats"):
        t.add_column(col, justify="right")
    t.add_column("Flags")
    t.add_column("Note")
    for o in options:
        a = o.offer
        t.add_row(
            a.date.isoformat(),
            _route(o),
            a.program_code,
            a.cabin.value,
            f"{a.miles:,}",
            f"${a.taxes_usd:,.0f}",
            f"{o.best_bank_points:,}" if o.best_bank_points else "-",
            o.best_bank.label if o.best_bank else "-",
            _other_bank_points(o),
            f"${o.cash_fare.price_usd:,.0f}" if o.cash_fare else "-",
            f"{o.cpp:.1f}" if o.cpp is not None else "-",
            "?" if a.seats is None else str(a.seats),
            "EST" if a.estimated else "",
            o.note or "",
        )
    return t


@app.command()
def search(
    origins: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. SJU"),
    dests: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. EUR"),
    dates: str = typer.Option(..., "--dates", help="YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD"),
    cabin: Cabin = typer.Option(Cabin.J, "--cabin", help="Y, W, J or F"),
    max_searches: int = typer.Option(40, "--max-searches", help="Cap on uncached live searches"),
    program: str = typer.Option("", "--program", help="Comma-separated program codes, e.g. AC,AV"),
    no_cash: bool = typer.Option(False, "--no-cash", help="Skip cash-fare lookups"),
    no_estimates: bool = typer.Option(False, "--no-estimates", help="Skip award-chart estimates"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore cached results"),
    limit: int = typer.Option(30, "--limit", help="Rows to show"),
    json_out: bool = typer.Option(False, "--json", help="Print the full SearchResult as JSON"),
) -> None:
    """Search award flights and rank them by bank points."""
    d_from, d_to = parse_dates(dates)
    query = SearchQuery(
        origins=_csv(origins),
        destinations=_csv(dests),
        date_from=d_from,
        date_to=d_to,
        cabin=cabin,
        max_searches=max_searches,
        programs=_csv(program) or None,
    )
    options = dict(refresh=refresh, include_estimates=not no_estimates, include_cash=not no_cash)
    try:
        if json_out:
            result = engine.search(query, **options)
        else:
            with Progress(
                TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), console=err_console
            ) as bar:
                task = bar.add_task("searching", total=None)

                def on_progress(done: int, total: int, message: str) -> None:
                    bar.update(task, completed=done, total=total, description=message[:60])

                result = engine.search(query, progress=on_progress, **options)
    except AuthError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    except ValueError as e:  # unknown region / airport
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if json_out:
        sys.stdout.reconfigure(encoding="utf-8")  # notes contain "−"; cp1252 can't encode it
        typer.echo(result.model_dump_json(indent=2))
        return
    if result.options:
        console.print(_options_table(result.options[:limit]))
    else:
        console.print("No matching offers.")
    console.print(
        f"{len(result.options)} options | searches: {result.searches_run} run, "
        f"{result.searches_cached} cached, {result.searches_skipped} skipped (cap {max_searches}), "
        f"{result.searches_planned} planned"
    )
    for e in result.errors:
        console.print(f"[red]error:[/red] {e}", markup=True, highlight=False)
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


@app.command()
def programs() -> None:
    """Transfer programs with Chase / Capital One ratios and verified dates."""
    t = Table()
    for col in ("Code", "Program", "Chase", "Capital One", "Verified"):
        t.add_column(col)
    for p in config.load_programs().values():
        chase, c1 = p.banks.get(Bank.chase), p.banks.get(Bank.capital_one)
        verified = min(r.verified for r in p.banks.values())
        t.add_row(
            p.code,
            p.name,
            chase.ratio_text if chase else "-",
            c1.ratio_text if c1 else "-",
            verified.isoformat(),
        )
    console.print(t)
    for code, bank, verified in config.stale_rows():
        console.print(f"[yellow]warning:[/yellow] {code} {bank.label} ratio last verified {verified} (over 90 days ago)")


@app.command()
def bonuses() -> None:
    """Active transfer bonuses."""
    active = config.load_bonuses()
    if not active:
        console.print("No active transfer bonuses.")
        return
    t = Table()
    for col in ("Bank", "Program", "Bonus", "Ends", "Source"):
        t.add_column(col)
    for b in active:
        t.add_row(b.bank.label, b.program, f"{b.bonus:.0%}", b.end.isoformat(), b.source or "")
    console.print(t)


@app.command("bonuses-update")
def bonuses_update(
    yes: bool = typer.Option(False, "--yes", help="Apply the changes without asking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the proposed changes, never write"),
) -> None:
    """Fetch public transfer-bonus trackers and propose changes to data/bonuses.yaml."""
    from . import bonus_updater as bu

    today = date.today()
    proposed, warnings, parsed = bu.collect(today=today)
    d = bu.diff(bu.read_all(bu.BONUSES_PATH), proposed, today, sources_parsed=parsed > 0)
    for w in warnings:
        console.print(f"[yellow]warning:[/yellow] {w}", highlight=False)
    if not d:
        console.print("No changes proposed.")
        return
    t = Table()
    for col in ("", "Bank", "Program", "Bonus", "Starts", "Ends", "Source"):
        t.add_column(col)
    for b in d.added:
        t.add_row("[green]+[/green]", b.bank.label, b.program, f"{b.bonus:.0%}", str(b.start), str(b.end), b.source or "")
    for b in d.removed:
        t.add_row("[red]-[/red]", b.bank.label, b.program, f"{b.bonus:.0%}", str(b.start), str(b.end), b.source or "")
    for old, new in d.changed:
        t.add_row(
            "[yellow]~[/yellow]", new.bank.label, new.program, f"{old.bonus:.0%} -> {new.bonus:.0%}",
            str(new.start), f"{old.end} -> {new.end}", new.source or "",
        )
    console.print(t)
    if dry_run:
        console.print("Dry run: nothing written.")
        return
    if not yes and not typer.confirm(f"Write these changes to {bu.BONUSES_PATH}?"):
        console.print("Nothing written.")
        return
    bu.apply(d, bu.BONUSES_PATH)
    console.print(f"Wrote {bu.BONUSES_PATH}")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Restart automatically when source files change"),
) -> None:
    """Run the local web dashboard."""
    from .web.app import serve

    serve(host, port, reload)


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (for Claude Code / Claude Desktop)."""
    from .mcp_server import main

    main()
