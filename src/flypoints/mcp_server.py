"""MCP server (stdio): thin wrappers over the engine, config and pricing modules."""
from __future__ import annotations

import asyncio
from datetime import date as _date

from mcp.server.mcpserver import Context, MCPServer

from . import config, engine, pricing
from .cli import _csv, _route
from .models import Bank, Cabin, RankedOption, SearchQuery
from .providers.base import AuthError, ProviderError
from .providers.cash import default_cash_provider

mcp = MCPServer("fly-points")


def _flatten(o: RankedOption) -> dict:
    a = o.offer
    return {
        "date": a.date.isoformat(),
        "route": _route(o),
        "program_code": a.program_code,
        "program_name": a.program_name,
        "cabin": a.cabin.value,
        "miles": a.miles,
        "taxes_usd": a.taxes_usd,
        "seats": a.seats,
        "best_bank": o.best_bank.label if o.best_bank else None,
        "best_bank_points": o.best_bank_points,
        "other_bank_points": {b.bank.label: b.bank_points for b in o.bank_options if b.bank != o.best_bank},
        "cash_price_usd": o.cash_fare.price_usd if o.cash_fare else None,
        "cpp": round(o.cpp, 2) if o.cpp is not None else None,
        "estimated": a.estimated,
        "booking_url": a.booking_url,
        "note": o.note,
    }


@mcp.tool()
async def search_awards(
    origins: str,
    destinations: str,
    date_from: str,
    date_to: str,
    cabin: str = "J",
    max_searches: int = 4,
    programs: str | None = None,
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

    origins / destinations: comma-separated IATA codes or region names (call list_regions to see
    them, e.g. "SJU" or "EUR"). date_from / date_to: YYYY-MM-DD. cabin: Y, W, J or F. programs:
    optional comma-separated program codes (e.g. "AC,AV"). Estimated rows (estimated=true) come
    from award charts, not live availability. On failure the result has an "error" key.
    """
    try:
        query = SearchQuery(
            origins=_csv(origins),
            destinations=_csv(destinations),
            date_from=_date.fromisoformat(date_from),
            date_to=_date.fromisoformat(date_to),
            cabin=Cabin(cabin.strip().upper()),
            max_searches=max_searches,
            programs=[p.upper() for p in _csv(programs)] if programs else None,
        )
    except ValueError as e:
        return {"error": f"Invalid input: {e}"}

    loop = asyncio.get_running_loop()

    def on_progress(done: int, total: int, message: str) -> None:
        # Called from engine worker threads; hand the coroutine to the event loop.
        if ctx is not None:
            asyncio.run_coroutine_threadsafe(ctx.report_progress(done, total, message), loop)

    try:
        result = await asyncio.to_thread(
            engine.search,
            query,
            progress=on_progress,
            refresh=refresh,
            include_estimates=include_estimates,
            include_cash=include_cash,
        )
    except AuthError as e:  # message already says how to refresh the token
        return {"error": str(e)}
    except ValueError as e:  # unknown region / airport
        return {"error": str(e)}
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
