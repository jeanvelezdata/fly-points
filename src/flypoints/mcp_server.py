"""MCP server (stdio): thin wrappers over the engine, config and pricing modules."""
from __future__ import annotations

import asyncio
from datetime import date as _date

from mcp.server.mcpserver import Context, MCPServer

from . import config, engine, pricing
from .booking import build_booking_plan
from .cli import _csv, _route
from .models import Bank, Cabin, RankedOption, SearchQuery
from .providers.base import AuthError, ProviderError
from .providers.cash import default_cash_provider
from .views import flatten_csv, option_times, parse_banks, parse_cabins, parse_days, seen_label

mcp = MCPServer("fly-points")


def _flatten(o: RankedOption) -> dict:
    a = o.offer
    depart, arrive = option_times(o) or (None, None)
    return {
        "date": a.date.isoformat(),
        "depart": depart,
        "arrive": arrive,
        "route": _route(o),
        "program_code": a.program_code,
        "program_name": a.program_name,
        "cabin": a.cabin.value,
        "mixed_cabin_pct": a.mixed_cabin_pct,
        "miles": a.miles,
        "taxes_usd": a.taxes_usd,
        "seats": a.seats,
        "best_bank": o.best_bank.label if o.best_bank else None,
        "best_bank_points": o.best_bank_points,
        "other_bank_points": {b.bank.label: b.bank_points for b in o.bank_options if b.bank != o.best_bank},
        "cash_price_usd": o.cash_fare.price_usd if o.cash_fare else None,
        "cpp": round(o.cpp, 2) if o.cpp is not None else None,
        "estimated": a.estimated,
        "seen": seen_label(a),
        "booking_url": a.booking_url,
        "note": o.note,
    }


def _query(origins, destinations, date_from, date_to, cabins, max_searches, programs, direct_only, max_stops,
           carriers, days, max_taxes_usd, max_points, min_seats, banks, min_cabin_pct) -> SearchQuery:
    """Build the SearchQuery from tool arguments; raises ValueError with a user-facing message."""
    return SearchQuery(
        origins=_csv(origins),
        destinations=_csv(destinations),
        date_from=_date.fromisoformat(date_from),
        date_to=_date.fromisoformat(date_to),
        cabins=parse_cabins(cabins or ["J"]),
        max_searches=max_searches,
        programs=[p.upper() for p in _csv(programs)] if programs else None,
        direct_only=direct_only,
        max_stops=max_stops,
        carriers=flatten_csv(carriers) or None,
        days_of_week=parse_days(days) or None,
        max_taxes_usd=max_taxes_usd,
        max_points=max_points,
        min_seats=min_seats,
        banks=parse_banks(banks) or None,
        min_cabin_pct=min_cabin_pct,
    )


def _progress(ctx: Context | None, loop: asyncio.AbstractEventLoop):
    def on_progress(done: int, total: int, message: str) -> None:
        # Called from engine worker threads; hand the coroutine to the event loop.
        if ctx is not None:
            asyncio.run_coroutine_threadsafe(ctx.report_progress(done, total, message), loop)

    return on_progress


async def _run_search(query: SearchQuery, stored, max_age_hours, include_cash, include_estimates, refresh, ctx):
    """Returns (SearchResult, None) or (None, error dict)."""
    try:
        if stored:
            result = await asyncio.to_thread(
                engine.search_stored, query, max_age_hours=max_age_hours, include_estimates=include_estimates
            )
        else:
            result = await asyncio.to_thread(
                engine.search,
                query,
                progress=_progress(ctx, asyncio.get_running_loop()),
                refresh=refresh,
                include_estimates=include_estimates,
                include_cash=include_cash,
            )
    except AuthError as e:  # message already says how to refresh the token
        return None, {"error": str(e)}
    except ValueError as e:  # unknown region / airport / dates out of range
        return None, {"error": str(e)}
    return result, None


@mcp.tool()
async def search_awards(
    origins: str,
    destinations: str,
    date_from: str,
    date_to: str,
    cabins: list[str] | None = None,
    max_searches: int = 4,
    programs: str | None = None,
    direct_only: bool = False,
    max_stops: int | None = None,
    carriers: list[str] | None = None,
    days: list[str] | None = None,
    max_taxes_usd: float | None = None,
    max_points: int | None = None,
    min_seats: int | None = None,
    banks: list[str] | None = None,
    min_cabin_pct: int | None = None,
    stored: bool = False,
    max_age_hours: float = engine.STORED_MAX_AGE_H,
    include_cash: bool = True,
    include_estimates: bool = True,
    refresh: bool = False,
    limit: int = 25,
    ctx: Context | None = None,
) -> dict:
    """Search award flights reachable with Chase Ultimate Rewards / Capital One miles, ranked by bank points.

    Each uncached live search takes about 30-40 seconds (PointsYeah, 4-day date windows), so keep
    max_searches small (default 4) and widen it only if the user asks. Cached searches are instant
    and do not count toward the cap; `searches_skipped` in the result says how many were cut off.
    With stored=true no live search runs: only results already in the local store from searches
    no older than max_age_hours (default 24) are used, so it is instant, and cash fares are not
    looked up; `warnings` says how much of the range has fresh coverage.

    origins / destinations: comma-separated IATA codes or region names (call list_regions to see
    them, e.g. "SJU" or "EUR"). date_from / date_to: YYYY-MM-DD. cabins: list of Y, W, J or F
    (default ["J"]). programs: optional comma-separated program codes (e.g. "AC,AV").
    Filters (all optional): direct_only; max_stops; carriers (operating carrier codes, every
    segment must match; estimates have no routing and are dropped); days (departure weekdays such
    as ["mon","fri"]); max_taxes_usd; max_points (best bank); min_seats (unknown seats pass);
    banks (["chase"], ["capital_one"] or both: only offers that bank can reach); min_cabin_pct
    (minimum % of the journey flown in the offer's cabin).
    Each option has `mixed_cabin_pct` (% flown BELOW the offer cabin; 0 = pure, null = unknown)
    and `seen` (age of the data). Estimated rows (estimated=true) come from award charts, not live
    availability. Options are ranked in order; use booking_plan with the same arguments and the
    option's 1-based position to get booking steps. On failure the result has an "error" key.
    """
    try:
        query = _query(origins, destinations, date_from, date_to, cabins, max_searches, programs, direct_only,
                       max_stops, carriers, days, max_taxes_usd, max_points, min_seats, banks, min_cabin_pct)
    except ValueError as e:
        return {"error": f"Invalid input: {e}"}

    result, error = await _run_search(query, stored, max_age_hours, include_cash, include_estimates, refresh, ctx)
    if error:
        return error
    return {
        "total_options": len(result.options),
        "searches_planned": result.searches_planned,
        "searches_run": result.searches_run,
        "searches_cached": result.searches_cached,
        "searches_skipped": result.searches_skipped,
        "errors": result.errors,
        "warnings": result.warnings,
        "options": [_flatten(o) for o in result.options[:limit]],
    }


@mcp.tool()
async def booking_plan(
    origins: str,
    destinations: str,
    date_from: str,
    date_to: str,
    index: int = 1,
    cabins: list[str] | None = None,
    max_searches: int = 4,
    programs: str | None = None,
    direct_only: bool = False,
    max_stops: int | None = None,
    carriers: list[str] | None = None,
    days: list[str] | None = None,
    max_taxes_usd: float | None = None,
    max_points: int | None = None,
    min_seats: int | None = None,
    banks: list[str] | None = None,
    min_cabin_pct: int | None = None,
    stored: bool = False,
    max_age_hours: float = engine.STORED_MAX_AGE_H,
    include_cash: bool = True,
    include_estimates: bool = True,
    ctx: Context | None = None,
) -> dict:
    """How to book one search result: ordered steps (verify, transfer, warn, book), alternatives and unknowns.

    Pass the same arguments as the search_awards call that produced the result, plus `index`, the
    option's 1-based position in that result (default 1, the top-ranked option). The search is
    repeated, so it is instant when the earlier search is cached or stored. Steps have kind
    verify / transfer / book / warning, text and an optional url. `unknowns` lists data that is
    not on file (phone numbers, hold policy, transfer times) and was deliberately not guessed.
    On failure the result has an "error" key.
    """
    try:
        query = _query(origins, destinations, date_from, date_to, cabins, max_searches, programs, direct_only,
                       max_stops, carriers, days, max_taxes_usd, max_points, min_seats, banks, min_cabin_pct)
    except ValueError as e:
        return {"error": f"Invalid input: {e}"}

    result, error = await _run_search(query, stored, max_age_hours, include_cash, include_estimates, False, ctx)
    if error:
        return error
    if not 1 <= index <= len(result.options):
        return {"error": f"index {index} is out of range: the search returned {len(result.options)} options."}
    option = result.options[index - 1]
    return {"option": _flatten(option), "plan": build_booking_plan(option).model_dump(mode="json")}


@mcp.tool()
async def sweep(
    origins: str,
    destinations: str,
    date_from: str,
    date_to: str,
    cabins: list[str] | None = None,
    max_searches: int = 8,
    fresh_hours: float = 24,
    ctx: Context | None = None,
) -> dict:
    """Fill the local result store with live searches for a (possibly year-long) date range.

    Nothing is ranked or returned except statistics; afterwards search_awards with stored=true
    answers instantly from what was collected. Each live search takes about 30-40 seconds, so the
    default max_searches is 8; windows already searched within fresh_hours are skipped without
    counting. Dates may be up to 365 days ahead. Result: planned, skipped_fresh, skipped_cap, run,
    errors, stopped. On failure the result has an "error" key.
    """
    try:
        cabin_list = parse_cabins(cabins or ["J"])
        d_from, d_to = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError as e:
        return {"error": f"Invalid input: {e}"}
    try:
        stats = await asyncio.to_thread(
            engine.sweep,
            _csv(origins),
            _csv(destinations),
            d_from,
            d_to,
            cabin_list,
            max_searches,
            fresh_hours,
            progress=_progress(ctx, asyncio.get_running_loop()),
        )
    except (AuthError, ValueError) as e:
        return {"error": str(e)}
    return stats.model_dump()


@mcp.tool()
def list_programs() -> dict:
    """Transfer programs with Chase / Capital One ratios and verified dates, plus stale-ratio warnings."""
    rows = []
    for p in config.load_programs().values():
        chase, c1 = p.banks.get(Bank.chase), p.banks.get(Bank.capital_one)
        rows.append(
            {
                "code": p.code,
                "name": p.name,
                "chase": chase.ratio_text if chase else None,
                "capital_one": c1.ratio_text if c1 else None,
                "verified": min(r.verified for r in p.banks.values()).isoformat(),
                "transfer_time": p.transfer_time,
            }
        )
    warnings = [
        f"{code} {bank.label} ratio last verified {verified} (over 90 days ago)"
        for code, bank, verified in config.stale_rows()
    ]
    return {"programs": rows, "warnings": warnings}


@mcp.tool()
def transfer_bonuses() -> dict:
    """Currently active bank-to-airline transfer bonuses."""
    return {
        "bonuses": [
            {
                "bank": b.bank.label,
                "program": b.program,
                "bonus_pct": round(b.bonus * 100),
                "ends": b.end.isoformat(),
                "source": b.source,
            }
            for b in config.load_bonuses()
        ]
    }


@mcp.tool()
def compare_cash(
    origin: str,
    destination: str,
    date: str,
    cabin: str = "J",
    miles: int | None = None,
    taxes_usd: float = 0,
    program: str | None = None,
) -> dict:
    """Cheapest cash fares (top 5) for one airport pair and day, to judge an award's value.

    date: YYYY-MM-DD. cabin: Y, W, J or F. If both miles and program (code, e.g. "AC") are given,
    also returns the bank points needed via Chase and Capital One and cents-per-point against the
    cheapest cash fare (taxes_usd are the award's cash taxes). On failure the result has an
    "error" key.
    """
    try:
        fares = sorted(
            default_cash_provider().fares(
                origin.strip().upper(), destination.strip().upper(), _date.fromisoformat(date), Cabin(cabin.strip().upper())
            ),
            key=lambda f: f.price_usd,
        )[:5]
    except (ValueError, ProviderError) as e:
        return {"error": str(e)}
    out: dict = {
        "fares": [
            {
                "price_usd": f.price_usd,
                "airlines": f.airlines,
                "flight_numbers": f.flight_numbers,
                "stops": f.stops,
                "provider": f.provider,
            }
            for f in fares
        ]
    }
    if miles and program:
        options = pricing.bank_options(
            program, miles, config.load_programs(), config.load_bank_rules(), config.load_bonuses()
        )
        cheapest = fares[0].price_usd if fares else None
        out["bank_points"] = {}
        for o in options:
            c = pricing.cpp(cheapest, taxes_usd, o.bank_points)
            out["bank_points"][o.bank.label] = {
                "points": o.bank_points,
                "bonus_pct": round(o.bonus * 100),
                "cpp": None if c is None else round(c, 2),
            }
        if not options:
            out["note"] = f"{program} is not a Chase / Capital One transfer partner in programs.yaml"
    return out


@mcp.tool()
def list_regions() -> dict:
    """Region names usable as origins/destinations in search_awards, with their airport codes."""
    return config.load_regions()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
