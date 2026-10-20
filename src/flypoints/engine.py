"""Search engine: expand airports x date windows, fan out to providers, cache, filter, rank.

CLI, MCP server and web app are thin layers over ``search()``.

Decisions:
- The ``max_searches`` cap applies to uncached *live* searches only. Chart (estimator) searches
  are offline and cheap, so they always run.
- Estimated offers carry ``date = date_from`` of their window; their cash fare is looked up for
  that date (a representative sample, not a fare for a real flight).
- Cash lookups served from the cache do not count toward ``max_cash_lookups``.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from pydantic import BaseModel, Field

from . import config, pricing
from .cache import Cache, make_key
from .models import AwardOffer, Cabin, CashFare, RankedOption, SearchQuery
from .providers.base import AuthError, AwardProvider, CashProvider, ProviderError
from .providers.cash import default_cash_provider
from .providers.pointsyeah import MAX_CONCURRENT, MIN_INTERVAL_S, PointsYeahProvider
from .ratelimit import RateLimiter

CHARTS = "charts"
LIVE_TTL_S = 6 * 3600
CHARTS_TTL_S = 30 * 24 * 3600
CASH_TTL_S = 12 * 3600
CASH_NAMESPACE = "cash"

Progress = Callable[[int, int, str], None]


class SearchResult(BaseModel):
    options: list[RankedOption] = Field(default_factory=list)
    searches_planned: int = 0
    searches_run: int = 0
    searches_cached: int = 0
    searches_skipped: int = 0  # uncached live searches beyond query.max_searches
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Search:
    provider: AwardProvider
    origin: str
    destination: str
    date_from: date
    date_to: date

    @property
    def is_chart(self) -> bool:
        return self.provider.name == CHARTS

    @property
    def label(self) -> str:
        return f"{self.provider.name} {self.origin}-{self.destination} {self.date_from}..{self.date_to}"


# --- planning -------------------------------------------------------------------------------


def _windows(date_from: date, date_to: date, days: int) -> list[tuple[date, date]]:
    out, start = [], date_from
    while start <= date_to:
        end = min(start + timedelta(days=days - 1), date_to)
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def _expand(tokens: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for token in tokens:
        for code in config.expand_airports(token):
            seen.setdefault(code)
    return list(seen)


def _plan(query: SearchQuery, providers: list[AwardProvider]) -> list[_Search]:
    """Chart searches first, then live searches in date order."""
    origins, destinations = _expand(query.origins), _expand(query.destinations)
    searches = [
        _Search(p, o, d, start, end)
        for p in providers
        for start, end in _windows(query.date_from, query.date_to, p.max_window_days)
        for o in origins
        for d in destinations
        if o != d
    ]
    return sorted(searches, key=lambda s: (not s.is_chart, s.date_from))


def _default_providers(cache: Cache, include_estimates: bool, warnings: list[str]) -> list[AwardProvider]:
    providers: list[AwardProvider] = [PointsYeahProvider(cache=cache)]
    if include_estimates:
        try:
            from .providers.charts import ChartEstimator

            providers.append(ChartEstimator())
        except Exception as e:  # missing module or broken chart data: estimates are optional
            warnings.append(f"Award-chart estimates skipped: {type(e).__name__}: {e}")
    return providers


# --- running searches -----------------------------------------------------------------------


def _search_key(s: _Search, cabin: Cabin) -> str:
    return make_key(s.origin, s.destination, s.date_from.isoformat(), s.date_to.isoformat(), cabin.value)


def _cached_offers(cache: Cache, s: _Search, cabin: Cabin) -> list[AwardOffer] | None:
    raw = cache.get_json(f"offers:{s.provider.name}", _search_key(s, cabin))
    return None if raw is None else [AwardOffer.model_validate(o) for o in raw]


def _store_offers(cache: Cache, s: _Search, cabin: Cabin, offers: list[AwardOffer]) -> None:
    ttl = CHARTS_TTL_S if s.is_chart else LIVE_TTL_S
    cache.put_json(
        f"offers:{s.provider.name}",
        _search_key(s, cabin),
        [o.model_dump(mode="json") for o in offers],
        ttl,
    )


def _run_searches(
    to_run: list[_Search],
    cache: Cache,
    cabin: Cabin,
    on_done: Callable[[str], None],
    result: SearchResult,
) -> list[AwardOffer]:
    """Run uncached searches in threads. Raises AuthError (aborting the rest) if one occurs."""
    limiters = {
        p.name: RateLimiter(0, MAX_CONCURRENT) if p.name == CHARTS else RateLimiter(MIN_INTERVAL_S, MAX_CONCURRENT)
        for p in {s.provider.name: s.provider for s in to_run}.values()
    }
    abort = threading.Event()

    def work(s: _Search) -> list[AwardOffer]:
        if abort.is_set():
            return []
        with limiters[s.provider.name]:
            offers = s.provider.search(s.origin, s.destination, s.date_from, s.date_to, cabin)
        _store_offers(cache, s, cabin, offers)
        return offers

    offers: list[AwardOffer] = []
    pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
    futures = {pool.submit(work, s): s for s in to_run}
    try:
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                offers += fut.result()
                result.searches_run += 1
            except AuthError:
                abort.set()
                raise
            except ProviderError as e:
                result.searches_run += 1
                result.errors.append(f"{s.label}: {e}")
            on_done(s.label)
    finally:
        pool.shutdown(wait=not abort.is_set(), cancel_futures=True)
    return offers


def _collect_offers(
    query: SearchQuery,
    providers: list[AwardProvider],
    cache: Cache,
    progress: Progress | None,
    refresh: bool,
    result: SearchResult,
) -> list[AwardOffer]:
    planned = _plan(query, providers)
    result.searches_planned = len(planned)

    offers: list[AwardOffer] = []
    to_run: list[_Search] = []
    live_budget = query.max_searches
    for s in planned:
        cached = None if refresh else _cached_offers(cache, s, query.cabin)
        if cached is not None:
            offers += cached
            result.searches_cached += 1
        elif s.is_chart:
            to_run.append(s)
        elif live_budget > 0:
            live_budget -= 1
            to_run.append(s)
        else:
            result.searches_skipped += 1

    total, done, lock = len(to_run) + result.searches_cached, 0, threading.Lock()

    def on_done(message: str) -> None:
        nonlocal done
        with lock:
            done += 1
            if progress:
                progress(done, total, message)

    for _ in range(result.searches_cached):
        on_done("cached")
    return offers + _run_searches(to_run, cache, query.cabin, on_done, result)


# --- filtering / ranking --------------------------------------------------------------------


def _filter_and_dedupe(
    offers: list[AwardOffer], query: SearchQuery, programs: dict[str, config.Program]
) -> list[AwardOffer]:
    wanted = {config.resolve_code(p, programs) for p in query.programs} if query.programs else None
    offers = [
        o
        for o in offers
        if o.cabin == query.cabin and (wanted is None or config.resolve_code(o.program_code, programs) in wanted)
    ]
    live_routes = {
        (config.resolve_code(o.program_code, programs), o.origin, o.destination)
        for o in offers
        if not o.estimated
    }
    offers = [
        o
        for o in offers
        if not (
            o.estimated
            and (config.resolve_code(o.program_code, programs), o.origin, o.destination) in live_routes
        )
    ]
    best: dict[tuple, AwardOffer] = {}
    for o in offers:
        old = best.get(o.key)
        if old is None or (o.miles, o.taxes_usd) < (old.miles, old.taxes_usd):
            best[o.key] = o
    return list(best.values())


def _cash_key(provider: CashProvider, key: tuple) -> str:
    return make_key(provider.name, *[str(k) for k in key])


def _attach_cash(
    ranked: list[RankedOption],
    provider: CashProvider,
    cache: Cache,
    max_lookups: int,
    refresh: bool,
    result: SearchResult,
    ctx: tuple,
) -> list[RankedOption]:
    """Cheapest cash fare per distinct (origin, destination, date, cabin), looked up in rank
    order. Cache hits are free; at most ``max_lookups`` uncached lookups hit the provider."""
    fares: dict[tuple, CashFare | None] = {}
    lookups = 0
    for opt in ranked:
        o = opt.offer
        key = (o.origin, o.destination, o.date, o.cabin)
        if key in fares:
            continue
        cache_key = _cash_key(provider, key)
        hit = None if refresh else cache.get_json(CASH_NAMESPACE, cache_key)
        if hit is not None:
            fares[key] = CashFare.model_validate(hit["fare"]) if hit["fare"] else None
            continue
        if lookups >= max_lookups:
            continue
        lookups += 1
        try:
            found = provider.fares(*key)
        except ProviderError as e:
            result.errors.append(f"cash {o.origin}-{o.destination} {o.date}: {e}")
            continue
        fare = min(found, key=lambda f: f.price_usd) if found else None
        cache.put_json(CASH_NAMESPACE, cache_key, {"fare": fare.model_dump(mode="json") if fare else None}, CASH_TTL_S)
        fares[key] = fare

    out = []
    for opt in ranked:
        o = opt.offer
        fare = fares.get((o.origin, o.destination, o.date, o.cabin))
        out.append(pricing.build_ranked_option(o, fare, *ctx) if fare else opt)
    return out


# --- public entry point ---------------------------------------------------------------------


def search(
    query: SearchQuery,
    *,
    award_providers: list[AwardProvider] | None = None,
    cash_provider: CashProvider | None = None,
    cache: Cache | None = None,
    progress: Progress | None = None,
    refresh: bool = False,
    include_estimates: bool = True,
    include_cash: bool = True,
    max_cash_lookups: int = 20,
    today: date | None = None,
) -> SearchResult:
    """Run a search. Raises AuthError if a provider's credentials are missing/expired; other
    ProviderErrors are recorded in ``SearchResult.errors``. ``progress(done, total, message)`` is
    called (thread-safely) after each search. ``refresh=True`` ignores cached reads."""
    result = SearchResult()
    cache = cache or Cache()
    if award_providers is None:
        award_providers = _default_providers(cache, include_estimates, result.warnings)
    elif not include_estimates:
        award_providers = [p for p in award_providers if p.name != CHARTS]

    result.warnings += [
        f"{code} {bank.label} ratio last verified {verified} (over 90 days ago)"
        for code, bank, verified in config.stale_rows(today=today)
    ]

    offers = _collect_offers(query, award_providers, cache, progress, refresh, result)

    programs, rules = config.load_programs(), config.load_bank_rules()
    ctx = (programs, rules, config.load_bonuses(today))
    offers = _filter_and_dedupe(offers, query, programs)
    ranked = pricing.rank([r for r in (pricing.build_ranked_option(o, None, *ctx) for o in offers) if r])

    if include_cash and ranked and max_cash_lookups > 0:
        provider = cash_provider or default_cash_provider()
        ranked = pricing.rank(_attach_cash(ranked, provider, cache, max_cash_lookups, refresh, result, ctx))
    result.options = ranked
    return result
