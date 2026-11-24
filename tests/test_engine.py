import threading
from datetime import date, datetime, timedelta

import pytest

from flypoints import engine
from flypoints.cache import Cache
from flypoints.models import AwardOffer, Bank, Cabin, CashFare, SearchQuery, Segment
from flypoints.providers.base import AuthError, ProviderError
from flypoints.store import OfferStore

NOW = datetime(2026, 10, 19, 12, 0)
TODAY = date(2026, 10, 19)


@pytest.fixture(autouse=True)
def fast_limiter(monkeypatch):
    monkeypatch.setattr(engine, "MIN_INTERVAL_S", 0)


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    yield c
    c.close()


@pytest.fixture
def store(tmp_path):
    st = OfferStore(tmp_path / "o.sqlite")
    yield st
    st.close()


def offer(code="AC", miles=60000, taxes=50.0, origin="SJU", dest="MAD", day=date(2026, 12, 2),
          cabin=Cabin.J, estimated=False):
    return AwardOffer(
        provider="fake", program_code=code, program_name=code, origin=origin, destination=dest,
        date=day, cabin=cabin, miles=miles, taxes_usd=taxes, estimated=estimated, fetched_at=NOW,
    )


class FakeAward:
    def __init__(self, name="fake", window=4, offers=None, error=None):
        self.name, self.max_window_days = name, window
        self.offers, self.error = offers or [], error
        self.calls = []
        self._lock = threading.Lock()

    def search(self, origin, destination, date_from, date_to, cabins):
        with self._lock:
            self.calls.append((origin, destination, date_from, date_to))
        if self.error:
            raise self.error
        return [o for o in self.offers if o.origin == origin and o.destination == destination]


class FakeCash:
    name = "fakecash"

    def __init__(self, price=3000.0, error=None):
        self.price, self.error, self.calls = price, error, []

    def fares(self, origin, destination, day, cabin):
        self.calls.append((origin, destination, day, cabin))
        if self.error:
            raise self.error

        def mk(p):
            return CashFare(provider="x", origin=origin, destination=destination, date=day,
                            cabin=cabin, price_usd=p, fetched_at=NOW)

        return [mk(self.price + 500), mk(self.price)]


def query(**kw):
    base = dict(origins=["SJU"], destinations=["MAD"], date_from=date(2026, 12, 1),
                date_to=date(2026, 12, 4), cabins=[Cabin.J])
    return SearchQuery(**{**base, **kw})


def run(q, providers, cache, store, **kw):
    kw.setdefault("include_cash", False)
    return engine.search(q, award_providers=providers, cache=cache, store=store, today=TODAY, **kw)


def test_region_and_window_expansion(cache, store):
    p = FakeAward()
    q = query(origins=["SJU"], destinations=["JPN"], date_to=date(2026, 12, 9))  # 9 days -> 3 windows
    r = run(q, [p], cache, store)
    assert r.searches_planned == 9 and r.searches_run == 9
    assert sorted({c[2:] for c in p.calls}) == [
        (date(2026, 12, 1), date(2026, 12, 4)),
        (date(2026, 12, 5), date(2026, 12, 8)),
        (date(2026, 12, 9), date(2026, 12, 9)),
    ]
    assert {c[1] for c in p.calls} == {"HND", "NRT", "KIX"}


def test_cap_skips_uncached_live_searches_in_date_order(cache, store):
    p = FakeAward()
    r = run(query(date_to=date(2026, 12, 12), max_searches=2), [p], cache, store)
    assert (r.searches_planned, r.searches_run, r.searches_skipped) == (3, 2, 1)
    assert sorted(c[2] for c in p.calls) == [date(2026, 12, 1), date(2026, 12, 5)]


def test_chart_searches_do_not_count_toward_cap(cache, store):
    live, charts = FakeAward(), FakeAward(name="charts", window=366)
    r = run(query(max_searches=1), [live, charts], cache, store)
    assert r.searches_run == 2 and r.searches_skipped == 0
    assert r.live_searches_run == 1  # what the web daily budget spends


def test_cache_hit_on_second_run_and_cached_do_not_count_toward_cap(cache, store):
    p = FakeAward(offers=[offer()])
    run(query(), [p], cache, store)
    r = run(query(max_searches=0), [p], cache, store)
    assert len(p.calls) == 1
    assert (r.searches_cached, r.searches_run, r.searches_skipped) == (1, 0, 0)
    assert len(r.options) == 1


def test_refresh_bypasses_cache(cache, store):
    p = FakeAward(offers=[offer()])
    run(query(), [p], cache, store)
    r = run(query(), [p], cache, store, refresh=True)
    assert len(p.calls) == 2 and r.searches_cached == 0


def test_progress_called_per_search(cache, store):
    seen = []
    run(query(date_to=date(2026, 12, 8)), [FakeAward()], cache, store, progress=lambda d, t, m: seen.append((d, t)))
    assert sorted(seen) == [(1, 2), (2, 2)]


def test_cabin_and_reachability_filter(cache, store):
    p = FakeAward(offers=[offer("AC"), offer("AC", cabin=Cabin.Y, miles=1), offer("AA", miles=1)])
    r = run(query(), [p], cache, store)
    assert [o.offer.program_code for o in r.options] == ["AC"]


def test_program_filter_resolves_aliases(cache, store):
    p = FakeAward(offers=[offer("KL"), offer("AC")])
    r = run(query(programs=["af"]), [p], cache, store)
    assert [o.offer.program_code for o in r.options] == ["KL"]


def test_dedupe_keeps_lowest_miles_then_taxes(cache, store):
    p = FakeAward(offers=[offer(miles=70000), offer(miles=60000, taxes=80), offer(miles=60000, taxes=40)])
    r = run(query(), [p], cache, store)
    assert [(o.offer.miles, o.offer.taxes_usd) for o in r.options] == [(60000, 40)]


def test_estimate_dropped_when_live_offer_exists_for_same_program_route(cache, store):
    live = FakeAward(offers=[offer("AC", miles=70000)])
    charts = FakeAward(name="charts", window=366,
                       offers=[offer("AC", miles=55000, estimated=True), offer("KL", miles=60000, estimated=True)])
    r = run(query(), [live, charts], cache, store)
    assert sorted((o.offer.program_code, o.offer.estimated) for o in r.options) == [("AC", False), ("KL", True)]


def test_include_estimates_false_drops_chart_provider(cache, store):
    charts = FakeAward(name="charts", window=366, offers=[offer("KL", estimated=True)])
    r = run(query(), [FakeAward(), charts], cache, store, include_estimates=False)
    assert charts.calls == [] and r.options == []


def test_auth_error_aborts(cache, store):
    p = FakeAward(error=AuthError("token expired"))
    with pytest.raises(AuthError, match="token expired"):
        run(query(date_to=date(2026, 12, 20)), [p], cache, store)


def test_other_provider_error_recorded_and_run_continues(cache, store):
    bad = FakeAward(name="bad", error=ProviderError("boom"))
    good = FakeAward(offers=[offer()])
    r = run(query(), [bad, good], cache, store)
    assert len(r.errors) == 1 and "boom" in r.errors[0] and "bad SJU-MAD" in r.errors[0]
    assert r.searches_run == 2 and len(r.options) == 1
    run(query(), [bad], cache, store)  # failures are not cached
    assert len(bad.calls) == 2


def test_ranking_by_bank_points(cache, store):
    p = FakeAward(offers=[offer("AC", miles=90000), offer("KL", miles=50000), offer("BA", miles=70000)])
    r = run(query(), [p], cache, store)
    assert [o.offer.program_code for o in r.options] == ["KL", "BA", "AC"]


def test_cash_attached_cheapest_fare_and_cpp(cache, store):
    p = FakeAward(offers=[offer("AC", miles=60000, taxes=50)])
    cash = FakeCash(price=3050.0)
    r = run(query(), [p], cache, store, cash_provider=cash, include_cash=True)
    o = r.options[0]
    assert o.cash_fare.price_usd == 3050.0
    assert o.cpp == pytest.approx((3050 - 50) / 60000 * 100)
    assert o.portal_points is not None
    r2 = run(query(), [p], cache, store, cash_provider=cash, include_cash=True)  # cash cached
    assert len(cash.calls) == 1 and r2.options[0].cash_fare is not None


def test_cash_lookup_cap_and_dedup(cache, store):
    offers = [offer("AC", day=date(2026, 12, d), miles=60000 + d) for d in (1, 2, 3, 4)]
    offers.append(offer("KL", day=date(2026, 12, 1), miles=70000))  # same route/date as first: one lookup
    cash = FakeCash()
    r = run(query(), [FakeAward(offers=offers)], cache, store, cash_provider=cash, include_cash=True, max_cash_lookups=2)
    assert len(cash.calls) == 2
    assert sum(o.cash_fare is not None for o in r.options) == 3  # 2 dates, KL shares the first date


def test_cash_error_recorded_not_fatal(cache, store):
    p = FakeAward(offers=[offer()])
    r = run(query(), [p], cache, store, cash_provider=FakeCash(error=ProviderError("nope")), include_cash=True)
    assert len(r.options) == 1 and r.options[0].cash_fare is None
    assert any("nope" in e for e in r.errors)


def test_cash_can_reorder_ties_by_cpp(cache, store):
    # equal bank points and taxes: higher cpp ranks first once cash is known
    p = FakeAward(offers=[offer("AC", miles=60000, day=date(2026, 12, 1)),
                          offer("AC", miles=60000, day=date(2026, 12, 2))])

    class PerDay(FakeCash):
        def fares(self, origin, destination, day, cabin):
            self.price = 5000.0 if day.day == 2 else 3000.0
            return super().fares(origin, destination, day, cabin)

    r = run(query(), [p], cache, store, cash_provider=PerDay(), include_cash=True)
    assert [o.offer.date.day for o in r.options] == [2, 1]


def test_estimator_import_failure_warns(cache, store, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "flypoints.providers.charts", None)
    warnings = []
    providers = engine._default_providers(cache, True, warnings)
    assert [p.name for p in providers] == ["pointsyeah"] and "estimates skipped" in warnings[0]


# --- multi-cabin, store-backed caching ------------------------------------------------------


class RecordingAward(FakeAward):
    def search(self, origin, destination, date_from, date_to, cabins):
        self.last_cabins = list(cabins)
        return super().search(origin, destination, date_from, date_to, cabins)


def test_all_requested_cabins_passed_to_provider_and_kept(cache, store):
    p = RecordingAward(offers=[offer(cabin=Cabin.J), offer(cabin=Cabin.Y, miles=30000, code="KL"),
                               offer(cabin=Cabin.F, miles=90000, code="BA")])
    r = run(query(cabins=[Cabin.J, Cabin.Y]), [p], cache, store)
    assert p.last_cabins == [Cabin.J, Cabin.Y]
    assert sorted(o.offer.cabin.value for o in r.options) == ["J", "Y"]


def test_second_run_served_from_store_but_different_cabins_are_not_covered(cache, store):
    p = FakeAward(offers=[offer()])
    run(query(), [p], cache, store)
    r = run(query(), [p], cache, store)
    assert len(p.calls) == 1 and r.searches_cached == 1 and len(r.options) == 1
    run(query(cabins=[Cabin.J, Cabin.Y]), [p], cache, store)
    assert len(p.calls) == 2  # Y was never searched


def test_store_replaces_window_when_space_disappears(cache, store):
    p = FakeAward(offers=[offer("AC"), offer("KL", miles=70000)])
    run(query(), [p], cache, store)
    p.offers = [offer("AC")]
    r = run(query(), [p], cache, store, refresh=True)
    assert [o.offer.program_code for o in r.options] == ["AC"]
    assert [o.offer.program_code for o in run(query(), [p], cache, store).options] == ["AC"]


def test_searched_nothing_counts_as_cached(cache, store):
    p = FakeAward()
    run(query(), [p], cache, store)
    r = run(query(max_searches=0), [p], cache, store)
    assert len(p.calls) == 1 and r.searches_cached == 1 and r.searches_skipped == 0


# --- dates ----------------------------------------------------------------------------------


def test_dates_must_be_between_today_and_a_year_out(cache, store):
    p = FakeAward()
    with pytest.raises(ValueError, match="in the past"):
        run(query(date_from=date(2026, 10, 1), date_to=date(2026, 10, 3)), [p], cache, store)
    with pytest.raises(ValueError, match="horizon"):
        run(query(date_to=date(2027, 10, 20)), [p], cache, store)
    with pytest.raises(ValueError, match="before the start"):
        run(query(date_from=date(2026, 12, 5)), [p], cache, store)
    assert p.calls == []
    run(query(date_from=date(2027, 10, 15), date_to=date(2027, 10, 19)), [FakeAward(window=366)], cache, store)


# --- filters --------------------------------------------------------------------------------


def seg(flight, a="SJU", b="MAD"):
    return Segment(flight_number=flight, origin=a, destination=b, depart=NOW, arrive=NOW)


def routed(code, flights, **kw):
    o = offer(code, **kw)
    stops = ["SJU", *[f"X{i}" for i in range(len(flights) - 1)], "MAD"]
    return o.model_copy(update={"segments": [seg(f, a, b) for f, a, b in zip(flights, stops, stops[1:])]})


def programs_after(cache, store, offers, **qkw):
    r = run(query(**qkw), [FakeAward(offers=offers)], cache, store, refresh=True)
    return sorted(o.offer.program_code for o in r.options)


def test_direct_only_and_max_stops(cache, store):
    offers = [routed("AC", ["AC1"]), routed("KL", ["KL1", "KL2"]), routed("BA", ["BA1", "BA2", "BA3"]),
              offer("TK", estimated=True)]  # no routing: unknown stops, filtered out
    assert programs_after(cache, store, offers) == ["AC", "BA", "KL", "TK"]
    assert programs_after(cache, store, offers, direct_only=True) == ["AC"]
    assert programs_after(cache, store, offers, max_stops=1) == ["AC", "KL"]
    assert programs_after(cache, store, offers, max_stops=1, direct_only=True) == ["AC"]


def test_carriers_require_every_segment(cache, store):
    offers = [routed("AC", ["AC1", "AC2"]), routed("KL", ["AC1", "KL2"]), routed("BA", ["BA1"])]
    assert programs_after(cache, store, offers, carriers=["ac"]) == ["AC"]
    assert programs_after(cache, store, offers, carriers=["AC", "KL"]) == ["AC", "KL"]


def test_days_of_week(cache, store):
    offers = [offer("AC", day=date(2026, 12, 2)), offer("KL", day=date(2026, 12, 3), miles=70000)]  # Wed, Thu
    assert programs_after(cache, store, offers, days_of_week=[2]) == ["AC"]
    assert programs_after(cache, store, offers, days_of_week=[2, 3]) == ["AC", "KL"]
    assert programs_after(cache, store, offers, days_of_week=[0]) == []


def test_max_taxes_and_min_seats_unknown_passes(cache, store):
    offers = [offer("AC", taxes=50.0), offer("KL", taxes=300.0)]
    assert programs_after(cache, store, offers, max_taxes_usd=100) == ["AC"]
    offers = [offer("AC").model_copy(update={"seats": 1}), offer("KL").model_copy(update={"seats": 4}), offer("BA")]
    assert programs_after(cache, store, offers, min_seats=2) == ["BA", "KL"]


def test_max_points_applies_to_best_bank_points(cache, store):
    offers = [offer("AC", miles=60000), offer("KL", miles=90000)]
    assert programs_after(cache, store, offers, max_points=60000) == ["AC"]
    assert programs_after(cache, store, offers, max_points=10) == []


def test_banks_restricts_reachability_and_best_bank(cache, store):
    offers = [offer("UA"), offer("AV", miles=70000), offer("B6", miles=30000)]  # chase-only, C1-only, both
    r = run(query(banks=[Bank.capital_one]), [FakeAward(offers=offers)], cache, store)
    assert sorted(o.offer.program_code for o in r.options) == ["AV", "B6"]
    assert {o.best_bank for o in r.options} == {Bank.capital_one}
    r = run(query(banks=[Bank.chase]), [FakeAward(offers=offers)], cache, store)
    assert sorted(o.offer.program_code for o in r.options) == ["B6", "UA"]
    assert {o.best_bank for o in r.options} == {Bank.chase}
    r = run(query(), [FakeAward(offers=offers)], cache, store)
    assert len(r.options) == 3


def test_min_cabin_pct_unknown_passes(cache, store):
    def mixed(code, pct):
        return offer(code).model_copy(update={"mixed_cabin_pct": pct})

    offers = [mixed("AC", 0), mixed("KL", 30), mixed("BA", 82), mixed("TK", None)]
    assert programs_after(cache, store, offers, min_cabin_pct=100) == ["AC", "TK"]
    assert programs_after(cache, store, offers, min_cabin_pct=70) == ["AC", "KL", "TK"]
    assert programs_after(cache, store, offers, min_cabin_pct=20) == ["AC", "KL", "TK"]


# --- search_stored --------------------------------------------------------------------------


def test_search_stored_never_calls_live_and_reports_coverage(cache, store, monkeypatch):
    live = FakeAward(offers=[offer("AC")])
    run(query(date_to=date(2026, 12, 8)), [live], cache, store)  # two windows, both stored
    stub = FakeAward()
    monkeypatch.setattr(engine, "PointsYeahProvider", lambda **kw: stub)
    r = engine.search_stored(query(date_to=date(2026, 12, 12)), cache=cache, store=store,
                             include_estimates=False, today=TODAY)
    assert stub.calls == [] and [o.offer.program_code for o in r.options] == ["AC"]
    assert any("2 of 3 route-windows have fresh coverage" in w and "1 with none" in w for w in r.warnings)
    assert r.searches_run == 0


def test_search_stored_max_age_and_empty_store(cache, store, monkeypatch):
    monkeypatch.setattr(engine, "PointsYeahProvider", lambda **kw: FakeAward())
    r = engine.search_stored(query(), cache=cache, store=store, include_estimates=False, today=TODAY)
    assert r.options == [] and "0 of 1 route-windows" in r.warnings[0]
    store.record_search("fake", "SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), [Cabin.J], [offer()],
                        datetime.now() - timedelta(hours=30))
    r = engine.search_stored(query(), cache=cache, store=store, include_estimates=False, today=TODAY)
    assert r.options == []
    r = engine.search_stored(query(), cache=cache, store=store, include_estimates=False, today=TODAY,
                             max_age_hours=48)
    assert len(r.options) == 1 and "1 of 1 route-windows" in r.warnings[0]


def test_search_stored_applies_filters_and_cash_only_if_asked(cache, store, monkeypatch):
    monkeypatch.setattr(engine, "PointsYeahProvider", lambda **kw: FakeAward())
    store.record_search("fake", "SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), [Cabin.J],
                        [offer("AC", taxes=500.0), offer("KL", miles=70000)], datetime.now())
    cash = FakeCash()
    r = engine.search_stored(query(max_taxes_usd=100), cache=cache, store=store, include_estimates=False,
                             cash_provider=cash, today=TODAY)
    assert [o.offer.program_code for o in r.options] == ["KL"] and cash.calls == []
    r = engine.search_stored(query(), cache=cache, store=store, include_estimates=False, cash_provider=cash,
                             include_cash=True, today=TODAY)
    assert len(cash.calls) == 1 and r.options[0].cash_fare is not None


# --- sweep ----------------------------------------------------------------------------------


def sweep(providers, cache, store, **kw):
    kw.setdefault("max_searches", 100)
    return engine.sweep(["SJU"], ["MAD"], date(2026, 12, 1), kw.pop("date_to", date(2026, 12, 12)), [Cabin.J],
                        award_providers=providers, cache=cache, store=store, today=TODAY, **kw)


def test_sweep_runs_records_and_skips_fresh_windows(cache, store, monkeypatch):
    monkeypatch.setattr(engine, "PointsYeahProvider", lambda **kw: FakeAward())
    p = FakeAward(offers=[offer()])
    s1 = sweep([p], cache, store)
    assert (s1.planned, s1.skipped_fresh, s1.run, s1.errors, s1.stopped) == (3, 0, 3, [], False)
    s2 = sweep([p], cache, store)
    assert (s2.planned, s2.skipped_fresh, s2.run) == (3, 3, 0) and len(p.calls) == 3
    s3 = sweep([p], cache, store, fresh_hours=0)  # nothing is fresh enough
    assert s3.run == 3 and len(p.calls) == 6
    assert len(engine.search_stored(query(), cache=cache, store=store, include_estimates=False,
                                    today=TODAY).options) == 1


def test_sweep_skips_windows_covered_by_a_differently_aligned_search(cache, store):
    store.record_search("fake", "SJU", "MAD", date(2026, 12, 1), date(2026, 12, 8), [Cabin.J], [], datetime.now())
    p = FakeAward()
    s = sweep([p], cache, store)
    assert (s.skipped_fresh, s.run) == (2, 1) and p.calls[0][2] == date(2026, 12, 9)


def test_sweep_max_searches_runs_earliest_stale_windows(cache, store):
    p = FakeAward()
    s = sweep([p], cache, store, max_searches=2)
    assert (s.planned, s.run, s.skipped_cap) == (3, 2, 1)
    assert sorted(c[2] for c in p.calls) == [date(2026, 12, 1), date(2026, 12, 5)]


def test_sweep_honors_stop_event(cache, store):
    stop = threading.Event()
    p = FakeAward()
    inner = p.search

    def search_then_stop(*a):
        out = inner(*a)
        stop.set()
        return out

    p.search = search_then_stop
    s = sweep([p], cache, store, date_to=date(2026, 12, 31), stop_event=stop)
    assert s.stopped and 1 <= s.run < s.planned
    pre = threading.Event()
    pre.set()
    q = FakeAward()
    assert sweep([q], cache, store, stop_event=pre).run == 0 and q.calls == []


def test_sweep_errors_recorded_and_auth_error_raises(cache, store):
    s = sweep([FakeAward(name="bad", error=ProviderError("boom"))], cache, store)
    assert s.run == 3 and len(s.errors) == 3 and "boom" in s.errors[0]
    with pytest.raises(AuthError):
        sweep([FakeAward(name="auth", error=AuthError("expired"))], cache, store)


def test_sweep_validates_dates_and_ignores_chart_providers(cache, store):
    with pytest.raises(ValueError, match="horizon"):
        sweep([FakeAward()], cache, store, date_to=date(2027, 11, 1))
    charts = FakeAward(name="charts", window=366)
    s = sweep([FakeAward(), charts], cache, store)
    assert charts.calls == [] and s.planned == 3
