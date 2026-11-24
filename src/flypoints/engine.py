"""Search engine: expand airports x date windows, fan out to providers, cache, filter, rank.

CLI, MCP server and web app are thin layers over ``search()``.

Decisions:
- The ``max_searches`` cap applies to uncached *live* searches only. Chart (estimator) searches
  are offline and cheap, so they always run.
- Estimated offers carry ``date = date_from`` of their window; their cash fare is looked up for
  that date (a representative sample, not a fare for a real flight).
- Cash lookups served from the cache do not count toward ``max_cash_lookups``.
- Live (non-chart) results live in the ``OfferStore``; a live window counts as cached when a
  search within ``LIVE_TTL_S`` covers every date and every requested cabin. Chart estimates use
  the JSON ``Cache``.
- Dates must fall in today..today+365 days (``MAX_HORIZON_DAYS``); anything else raises ValueError.
- Stop/route filters that need routing (stops, carriers) drop offers without segments, i.e. chart
  estimates, because the answer is unknown.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from pydantic import BaseModel, Field

from . import config, pricing
from .cache import Cache, make_key
from .models import AwardOffer, Cabin, CashFare, OptionFilters, RankedOption, SearchQuery
from .providers.base import AuthError, AwardProvider, CashProvider, ProviderError
from .providers.cash import default_cash_provider
from .providers.pointsyeah import MAX_CONCURRENT, MIN_INTERVAL_S, PointsYeahProvider
from .ratelimit import RateLimiter
from .store import OfferStore

CHARTS = "charts"
LIVE_TTL_S = 6 * 3600
CHARTS_TTL_S = 30 * 24 * 3600
CASH_TTL_S = 12 * 3600
CASH_NAMESPACE = "cash"
MAX_HORIZON_DAYS = 365
STORED_MAX_AGE_H = 24

Progress = Callable[[int, int, str], None]


class SearchResult(BaseModel):
    options: list[RankedOption] = Field(default_factory=list)
    searches_planned: int = 0
    searches_run: int = 0
    live_searches_run: int = 0  # of searches_run, the ones that called a live provider (not charts)
    searches_cached: int = 0
    searches_skipped: int = 0  # uncached live searches beyond query.max_searches
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SweepStats(BaseModel):
    planned: int = 0  # live windows in the sweep
    skipped_fresh: int = 0  # already covered by a search within fresh_hours
    skipped_cap: int = 0  # stale windows beyond max_searches
    run: int = 0  # windows attempted, including ones that ended in an error
    errors: list[str] = Field(default_factory=list)
    stopped: bool = False  # stop_event was set before every selected window ran


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
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    @property
    def label(self) -> str:
        return f"{self.provider.name} {self.origin}-{self.destination} {self.date_from}..{self.date_to}"


# --- planning -------------------------------------------------------------------------------


def _check_dates(date_from: date, date_to: date, today: date) -> None:
    last = today + timedelta(days=MAX_HORIZON_DAYS)
    if date_from < today:
        raise ValueError(f"Start date {date_from} is in the past (today is {today}).")
    if date_to < date_from:
        raise ValueError(f"End date {date_to} is before the start date {date_from}.")
    if date_to > last:
        raise ValueError(f"End date {date_to} is beyond the {MAX_HORIZON_DAYS}-day horizon (last allowed: {last}).")


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


def _search_key(s: _Search, cabins: list[Cabin]) -> str:
    codes = sorted(c.value for c in cabins)
    return make_key(s.origin, s.destination, s.date_from.isoformat(), s.date_to.isoformat(), codes)


def _cached_offers(cache: Cache, s: _Search, cabins: list[Cabin]) -> list[AwardOffer] | None:
    """Chart estimates only; live offers come from the OfferStore."""
    raw = cache.get_json(f"offers:{s.provider.name}", _search_key(s, cabins))
    return None if raw is None else [AwardOffer.model_validate(o) for o in raw]


def _store_offers(cache: Cache, s: _Search, cabins: list[Cabin], offers: list[AwardOffer]) -> None:
    cache.put_json(
        f"offers:{s.provider.name}",
        _search_key(s, cabins),
        [o.model_dump(mode="json") for o in offers],
        CHARTS_TTL_S,
    )


def _covered_days(store: OfferStore, s: _Search, cabins: list[Cabin], max_age_s: float) -> int:
    return len(store.coverage(s.origin, s.destination, s.date_from, s.date_to, cabins, max_age_s, provider=s.provider.name))


def _stored_offers(store: OfferStore, s: _Search, cabins: list[Cabin], max_age_s: float) -> list[AwardOffer]:
    return store.query([s.origin], [s.destination], s.date_from, s.date_to, cabins, max_age_s, provider=s.provider.name)


def _run_searches(
    to_run: list[_Search],
    cache: Cache,
    store: OfferStore,
    cabins: list[Cabin],
    on_done: Callable[[str], None],
    result: SearchResult,
    stop: threading.Event | None = None,
) -> list[AwardOffer]:
    """Run uncached searches in threads. Raises AuthError (aborting the rest) if one occurs.
    Once ``stop`` is set, searches that have not started are dropped and not counted as run."""
    limiters = {
        p.name: RateLimiter(0, MAX_CONCURRENT) if p.name == CHARTS else RateLimiter(MIN_INTERVAL_S, MAX_CONCURRENT)
        for p in {s.provider.name: s.provider for s in to_run}.values()
    }
    abort = threading.Event()

    def stopped() -> bool:
        return abort.is_set() or (stop is not None and stop.is_set())

    def work(s: _Search) -> list[AwardOffer] | None:
        if stopped():
            return None
        with limiters[s.provider.name]:
            if stopped():
                return None
            offers = s.provider.search(s.origin, s.destination, s.date_from, s.date_to, cabins)
        if s.is_chart:
            _store_offers(cache, s, cabins, offers)
        else:
            store.record_search(
                s.provider.name, s.origin, s.destination, s.date_from, s.date_to, cabins, offers, datetime.now()
            )
        return offers

    offers: list[AwardOffer] = []
    pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
    futures = {pool.submit(work, s): s for s in to_run}
    try:
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                found = fut.result()
                if found is None:
                    continue
                offers += found
                result.searches_run += 1
                result.live_searches_run += not s.is_chart
            except AuthError:
                abort.set()
                raise
            except ProviderError as e:
                result.searches_run += 1
                result.live_searches_run += not s.is_chart
                result.errors.append(f"{s.label}: {e}")
            on_done(s.label)
    finally:
        pool.shutdown(wait=not abort.is_set(), cancel_futures=True)
    return offers


def _collect_offers(
    query: SearchQuery,
    providers: list[AwardProvider],
    cache: Cache,
    store: OfferStore,
    progress: Progress | None,
    refresh: bool,
    result: SearchResult,
    stored_only: bool = False,
    max_age_s: float = LIVE_TTL_S,
) -> list[AwardOffer]:
    """With ``stored_only`` live windows are read from the store and never searched."""
    planned = _plan(query, providers)
    result.searches_planned = len(planned)

    offers: list[AwardOffer] = []
    to_run: list[_Search] = []
    live_budget = query.max_searches
    fresh = partial = none = 0
    for s in planned:
        if s.is_chart:
            cached = None if refresh else _cached_offers(cache, s, query.cabins)
            if cached is not None:
                offers += cached
                result.searches_cached += 1
            else:
                to_run.append(s)
            continue
        covered = 0 if refresh and not stored_only else _covered_days(store, s, query.cabins, max_age_s)
        if stored_only or covered == s.days:
            offers += _stored_offers(store, s, query.cabins, max_age_s)
            if covered == s.days:
                fresh += 1
                result.searches_cached += 1
            elif covered:
                partial += 1
            else:
                none += 1
        elif live_budget > 0:
            live_budget -= 1
            to_run.append(s)
        else:
            result.searches_skipped += 1

    if stored_only:
        result.warnings.append(
            f"Stored results only (no live searches): {fresh} of {fresh + partial + none} route-windows have "
            f"fresh coverage (searched within {max_age_s / 3600:g}h), {partial} partial, {none} with none."
        )

    total, done, lock = len(to_run) + result.searches_cached, 0, threading.Lock()

    def on_done(message: str) -> None:
        nonlocal done
        with lock:
            done += 1
            if progress:
                progress(done, total, message)

    for _ in range(result.searches_cached):
        on_done("cached")
    return offers + _run_searches(to_run, cache, store, query.cabins, on_done, result)


# --- filtering / ranking --------------------------------------------------------------------


def _filter_and_dedupe(
    offers: list[AwardOffer], query: SearchQuery, programs: dict[str, config.Program]
) -> list[AwardOffer]:
    wanted = {config.resolve_code(p, programs) for p in query.programs} if query.programs else None
    offers = [
        o
        for o in offers
        if o.cabin in query.cabins and (wanted is None or config.resolve_code(o.program_code, programs) in wanted)
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


def filter_options(options: list[RankedOption], f: OptionFilters) -> list[RankedOption]:
    """Apply the optional result filters to priced options (``f.banks`` is applied at pricing,
    where it also decides best_bank). Order is preserved."""
    max_stops = min((x for x in (0 if f.direct_only else None, f.max_stops) if x is not None), default=None)

    def keep(opt: RankedOption) -> bool:
        o = opt.offer
        if max_stops is not None and (not o.segments or len(o.segments) - 1 > max_stops):
            return False
        if f.carriers is not None and (
            not o.segments or any(s.flight_number[:2].upper() not in f.carriers for s in o.segments)
        ):
            return False
        if f.days_of_week is not None and o.date.weekday() not in f.days_of_week:
            return False
        if f.max_taxes_usd is not None and o.taxes_usd > f.max_taxes_usd:
            return False
        if f.max_points is not None and (opt.best_bank_points is None or opt.best_bank_points > f.max_points):
            return False
        if f.min_seats is not None and o.seats is not None and o.seats < f.min_seats:
            return False
        if f.min_cabin_pct is not None and o.mixed_cabin_pct is not None and 100 - o.mixed_cabin_pct < f.min_cabin_pct:
            return False
        return True

    return [opt for opt in options if keep(opt)]


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


def _rank_and_price(
    result: SearchResult,
    offers: list[AwardOffer],
    query: SearchQuery,
    cache: Cache,
    cash_provider: CashProvider | None,
    include_cash: bool,
    max_cash_lookups: int,
    refresh: bool,
    today: date | None,
) -> None:
    """Dedupe, price through the banks, filter, rank, attach cash; sets ``result.options``."""
    programs, rules = config.load_programs(), config.load_bank_rules()
    ctx = (programs, rules, config.load_bonuses(today), query.banks)
    offers = _filter_and_dedupe(offers, query, programs)
    priced = [r for r in (pricing.build_ranked_option(o, None, *ctx) for o in offers) if r]
    ranked = pricing.rank(filter_options(priced, query))

    if include_cash and ranked and max_cash_lookups > 0:
        provider = cash_provider or default_cash_provider()
        ranked = pricing.rank(_attach_cash(ranked, provider, cache, max_cash_lookups, refresh, result, ctx))
    result.options = ranked


# --- public entry points --------------------------------------------------------------------


def _open_store(store: OfferStore | None, today: date) -> OfferStore:
    if store is not None:
        return store
    store = OfferStore()
    store.prune(today)
    return store


def search(
    query: SearchQuery,
    *,
    award_providers: list[AwardProvider] | None = None,
    cash_provider: CashProvider | None = None,
    cache: Cache | None = None,
    store: OfferStore | None = None,
    progress: Progress | None = None,
    refresh: bool = False,
    include_estimates: bool = True,
    include_cash: bool = True,
    max_cash_lookups: int = 20,
    today: date | None = None,
) -> SearchResult:
    """Run a search. Raises AuthError if a provider's credentials are missing/expired and
    ValueError for unknown airports or dates outside today..today+365; other ProviderErrors are
    recorded in ``SearchResult.errors``. ``progress(done, total, message)`` is called
    (thread-safely) after each search. ``refresh=True`` ignores cached reads."""
    _check_dates(query.date_from, query.date_to, today or date.today())
    result = SearchResult()
    cache = cache or Cache()
    store = _open_store(store, today or date.today())
    if award_providers is None:
        award_providers = _default_providers(cache, include_estimates, result.warnings)
    elif not include_estimates:
        award_providers = [p for p in award_providers if p.name != CHARTS]

    result.warnings += [
        f"{code} {bank.label} ratio last verified {verified} (over 90 days ago)"
        for code, bank, verified in config.stale_rows(today=today)
    ]

    offers = _collect_offers(query, award_providers, cache, store, progress, refresh, result)
    _rank_and_price(result, offers, query, cache, cash_provider, include_cash, max_cash_lookups, refresh, today)
    return result


def search_stored(
    query: SearchQuery,
    *,
    max_age_hours: float = STORED_MAX_AGE_H,
    cash_provider: CashProvider | None = None,
    cache: Cache | None = None,
    store: OfferStore | None = None,
    include_estimates: bool = True,
    include_cash: bool = False,
    max_cash_lookups: int = 20,
    today: date | None = None,
) -> SearchResult:
    """Instant search: offers come only from the OfferStore (searches no older than
    ``max_age_hours``); no live provider is called. Chart estimates (offline) and, if
    ``include_cash``, cash lookups still run. ``warnings`` says how many route-windows have
    fresh coverage."""
    _check_dates(query.date_from, query.date_to, today or date.today())
    result = SearchResult()
    cache = cache or Cache()
    store = _open_store(store, today or date.today())
    providers: list[AwardProvider] = [PointsYeahProvider(cache=cache)]  # only for its name and window size
    if include_estimates:
        _, *charts = _default_providers(cache, True, result.warnings)
        providers += charts
    offers = _collect_offers(
        query, providers, cache, store, None, False, result, stored_only=True, max_age_s=max_age_hours * 3600
    )
    _rank_and_price(result, offers, query, cache, cash_provider, include_cash, max_cash_lookups, False, today)
    return result


def sweep(
    origins: list[str],
    destinations: list[str],
    date_from: date,
    date_to: date,
    cabins: list[Cabin],
    max_searches: int = 40,
    fresh_hours: float = 24,
    progress: Progress | None = None,
    stop_event: threading.Event | None = None,
    *,
    award_providers: list[AwardProvider] | None = None,
    cache: Cache | None = None,
    store: OfferStore | None = None,
    today: date | None = None,
) -> SweepStats:
    """Fill the OfferStore for a (possibly year-long) range: plan the live windows, skip those a
    search within ``fresh_hours`` already covers, run at most ``max_searches`` of the rest in date
    order. Results are only recorded, not ranked. Set ``stop_event`` to stop between searches
    (those in flight finish). Raises AuthError like ``search``."""
    today = today or date.today()
    query = SearchQuery(
        origins=origins, destinations=destinations, date_from=date_from, date_to=date_to,
        cabins=cabins, max_searches=max_searches,
    )
    _check_dates(date_from, date_to, today)
    cache = cache or Cache()
    store = _open_store(store, today)
    providers = [p for p in (award_providers or [PointsYeahProvider(cache=cache)]) if p.name != CHARTS]
    planned = _plan(query, providers)

    stats = SweepStats(planned=len(planned))
    stale = []
    for s in planned:
        if _covered_days(store, s, query.cabins, fresh_hours * 3600) == s.days:
            stats.skipped_fresh += 1
        else:
            stale.append(s)
    to_run = stale[: max(max_searches, 0)]
    stats.skipped_cap = len(stale) - len(to_run)

    done, lock = 0, threading.Lock()

    def on_done(message: str) -> None:
        nonlocal done
        with lock:
            done += 1
            if progress:
                progress(done, len(to_run), message)

    result = SearchResult()
    _run_searches(to_run, cache, store, query.cabins, on_done, result, stop_event)
    stats.run, stats.errors = result.searches_run, result.errors
    stats.stopped = stop_event is not None and stop_event.is_set() and stats.run < len(to_run)
    return stats
