"""Typer CLI: a thin layer over ``flypoints.engine.search``."""
from __future__ import annotations

import sys
import threading
from datetime import date, datetime

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table

from . import booking, config, engine
from .booking import BookingPlan
from .models import Bank, Cabin, RankedOption, SearchQuery
from .providers.base import AuthError
from .views import (
    compare_grid, mixed_cabin_marker, option_route as _route, option_times, parse_banks, parse_cabins,
    parse_days, seen_label, segment_lines,
)

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


def _other_bank_points(o: RankedOption) -> str:
    others = [f"{b.bank.label} {b.bank_points:,}" for b in o.bank_options if b.bank != o.best_bank]
    return ", ".join(others) or "-"


def _options_table(options: list[RankedOption]) -> Table:
    t = Table(show_lines=False)
    t.add_column("#", justify="right")
    for col in ("Date", "Depart", "Arrive", "Route", "Program", "Cabin"):
        t.add_column(col)
    for col in ("Miles", "Taxes", "Best bank pts"):
        t.add_column(col, justify="right")
    t.add_column("Best bank")
    t.add_column("Other bank")
    for col in ("Cash", "cpp", "Seats"):
        t.add_column(col, justify="right")
    t.add_column("Seen")
    t.add_column("Flags")
    t.add_column("Note")
    for n, o in enumerate(options, 1):
        a = o.offer
        marker, detail = mixed_cabin_marker(a)
        depart, arrive = option_times(o) or ("-", "-")
        t.add_row(
            str(n),
            a.date.isoformat(),
            depart,
            arrive,
            _route(o),
            a.program_code,
            f"{a.cabin.value}{marker}" + (f" ({detail})" if detail and marker == "*" else ""),
            f"{a.miles:,}",
            f"${a.taxes_usd:,.0f}",
            f"{o.best_bank_points:,}" if o.best_bank_points else "-",
            o.best_bank.label if o.best_bank else "-",
            _other_bank_points(o),
            f"${o.cash_fare.price_usd:,.0f}" if o.cash_fare else "-",
            f"{o.cpp:.1f}" if o.cpp is not None else "-",
            "?" if a.seats is None else str(a.seats),
            seen_label(a),
            "EST" if a.estimated else "",
            o.note or "",
        )
    return t


def _seats(c) -> str:
    return "?" if c.seats is None else str(c.seats)


def _compare_table(options: list[RankedOption]) -> Table:
    """One row per program/route/date; per cabin the best bank points, miles and seats."""
    t = Table(show_lines=False)
    for col in ("Date", "Route", "Program"):
        t.add_column(col)
    for cabin in Cabin:
        t.add_column(cabin.value, justify="right")
    for row in compare_grid(options):
        cells = []
        for cabin in Cabin:
            c = row.cells.get(cabin)
            if c is None:
                cells.append("-")
                continue
            price = f"{'~' if c.estimated else ''}{c.points:,} {c.bank}\n" if c.points else ""
            cells.append(f"{price}{c.miles:,} mi, {_seats(c)} seats")
        t.add_row(row.date.isoformat(), row.route, row.program_code, *cells)
    return t


_STEP_STYLE = {"verify": "cyan", "transfer": "green", "book": "bold", "warning": "yellow"}


def _booking_panel(plan: BookingPlan, option: RankedOption, number: int) -> Panel:
    lines = [escape(f) for f in segment_lines(option)]
    if lines:
        lines.append("[dim](local airport times)[/dim]\n")
    for n, step in enumerate(plan.steps, 1):
        lines.append(f"[{_STEP_STYLE[step.kind]}]{n}. {step.kind.upper()}:[/] {escape(step.text)}")
        if step.url:
            lines.append(f"     {escape(step.url)}")
    if plan.alternatives:
        lines.append("\n[bold]Alternatives[/bold]")
        for alt in plan.alternatives:
            banks = ", ".join(b.label for b in alt.banks)
            lines.append(f"  {alt.program_code} {escape(alt.program_name)} (via {banks}): {escape(alt.reason)}")
    if plan.unknowns:
        lines.append("\n[bold]Unknown (not guessed)[/bold]")
        lines += [f"  - {escape(u)}" for u in plan.unknowns]
    a = option.offer
    title = f"How to book #{number}: {a.program_code} {_route(option)} {a.date.isoformat()} {a.cabin.value}"
    return Panel("\n".join(lines), title=title, title_align="left")


@app.command()
def search(
    origins: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. SJU"),
    dests: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. EUR"),
    dates: str = typer.Option(..., "--dates", help="YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD"),
    cabin: list[str] = typer.Option(["J"], "--cabin", help="Y, W, J or F; comma-separated or repeated, e.g. Y,J"),
    max_searches: int = typer.Option(40, "--max-searches", help="Cap on uncached live searches"),
    program: str = typer.Option("", "--program", help="Comma-separated program codes, e.g. AC,AV"),
    direct: bool = typer.Option(False, "--direct", help="Nonstop only"),
    max_stops: int | None = typer.Option(None, "--max-stops", help="At most this many stops"),
    carriers: str = typer.Option("", "--carriers", help="Operating carriers, e.g. AC,UA (every segment must match)"),
    days: str = typer.Option("", "--days", help="Departure weekdays, e.g. mon,fri"),
    max_taxes: float | None = typer.Option(None, "--max-taxes", help="Max cash taxes in USD"),
    max_points: int | None = typer.Option(None, "--max-points", help="Max bank points (best bank)"),
    min_seats: int | None = typer.Option(None, "--min-seats", help="Min seats; unknown counts pass"),
    bank: list[str] = typer.Option([], "--bank", help="chase or capital_one; comma-separated or repeated"),
    min_cabin_pct: int | None = typer.Option(None, "--min-cabin-pct", help="Min % of the journey in the offer's cabin"),
    stored: bool = typer.Option(False, "--stored", help="Instant: use only stored results, no live searches"),
    max_age_hours: float = typer.Option(engine.STORED_MAX_AGE_H, "--max-age-hours", help="With --stored: oldest search to use"),
    view: str = typer.Option("list", "--view", help="list, or compare (one row per program/route/date, Y/W/J/F columns)"),
    book: int | None = typer.Option(None, "--book", help="Also print how to book result #N of the list view"),
    no_cash: bool = typer.Option(False, "--no-cash", help="Skip cash-fare lookups"),
    no_estimates: bool = typer.Option(False, "--no-estimates", help="Skip award-chart estimates"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore cached results"),
    limit: int = typer.Option(30, "--limit", help="Rows to show"),
    json_out: bool = typer.Option(False, "--json", help="Print the full SearchResult as JSON"),
) -> None:
    """Search award flights and rank them by bank points."""
    d_from, d_to = parse_dates(dates)
    if view not in ("list", "compare"):
        raise typer.BadParameter("use list or compare", param_hint="--view")
    try:
        query = SearchQuery(
            origins=_csv(origins),
            destinations=_csv(dests),
            date_from=d_from,
            date_to=d_to,
            cabins=parse_cabins(cabin),
            max_searches=max_searches,
            programs=_csv(program) or None,
            direct_only=direct,
            max_stops=max_stops,
            carriers=_csv(carriers) or None,
            days_of_week=parse_days(days) or None,
            max_taxes_usd=max_taxes,
            max_points=max_points,
            min_seats=min_seats,
            banks=parse_banks(bank) or None,
            min_cabin_pct=min_cabin_pct,
        )
    except ValueError as e:  # bad cabin / day / bank / filter value
        raise typer.BadParameter(str(e)) from None
    options = dict(refresh=refresh, include_estimates=not no_estimates, include_cash=not no_cash)
    try:
        if stored:
            result = engine.search_stored(query, max_age_hours=max_age_hours, include_estimates=not no_estimates)
        elif json_out:
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
    if book is not None and not 1 <= book <= len(result.options):
        err_console.print(f"[red]--book {book}: there are {len(result.options)} results.[/red]")
        raise typer.Exit(1)
    if result.options:
        shown = result.options[:limit]
        console.print(_compare_table(shown) if view == "compare" else _options_table(shown))
    else:
        console.print("No matching offers.")
    searches = "stored results only" if stored else (
        f"searches: {result.searches_run} run, {result.searches_cached} cached, "
        f"{result.searches_skipped} skipped (cap {max_searches}), {result.searches_planned} planned"
    )
    console.print(f"{len(result.options)} options | {searches}")
    for e in result.errors:
        console.print(f"[red]error:[/red] {e}", markup=True, highlight=False)
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")
    if book is not None:
        option = result.options[book - 1]
        console.print(_booking_panel(booking.build_booking_plan(option), option, book))


@app.command()
def sweep(
    origins: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. SJU"),
    dests: str = typer.Argument(..., help="Comma-separated IATA codes or region names, e.g. EUR"),
    dates: str = typer.Option(..., "--dates", help="YYYY-MM-DD:YYYY-MM-DD, up to a year ahead"),
    cabin: list[str] = typer.Option(["J"], "--cabin", help="Y, W, J or F; comma-separated or repeated, e.g. Y,J"),
    max_searches: int = typer.Option(40, "--max-searches", help="Cap on live searches this run"),
    fresh_hours: float = typer.Option(24, "--fresh-hours", help="Skip windows searched within this many hours"),
) -> None:
    """Fill the local store with live searches for a long date range (Ctrl-C stops cleanly)."""
    d_from, d_to = parse_dates(dates)
    try:
        cabins = parse_cabins(cabin)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    stop = threading.Event()
    outcome: dict = {}
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), console=err_console) as bar:
        task = bar.add_task("sweeping", total=None)

        def on_progress(done: int, total: int, message: str) -> None:
            bar.update(task, completed=done, total=total, description=message[:60])

        def work() -> None:
            try:
                outcome["stats"] = engine.sweep(
                    _csv(origins), _csv(dests), d_from, d_to, cabins, max_searches, fresh_hours,
                    progress=on_progress, stop_event=stop,
                )
            except Exception as e:  # reported below, on the main thread
                outcome["error"] = e

        worker = threading.Thread(target=work, name="flypoints-sweep")
        try:
            worker.start()
            while worker.is_alive():
                worker.join(0.2)
        except KeyboardInterrupt:
            stop.set()
            bar.update(task, description="stopping after the searches in flight...")
            worker.join()
    error = outcome.get("error")
    if isinstance(error, AuthError):
        err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(2)
    if isinstance(error, ValueError):  # unknown region / airport / dates outside the horizon
        err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(1)
    if error is not None:
        raise error
    stats = outcome["stats"]
    console.print(
        f"{stats.planned} windows planned | {stats.skipped_fresh} already fresh, {stats.skipped_cap} over the cap "
        f"({max_searches}), {stats.run} run" + (" | stopped early" if stats.stopped else "")
    )
    for e in stats.errors:
        console.print(f"[red]error:[/red] {e}", markup=True, highlight=False)


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
