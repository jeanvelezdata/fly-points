import threading
from datetime import date, datetime

import pytest

from flypoints import engine
from flypoints.cache import Cache
from flypoints.models import AwardOffer, Cabin, CashFare, SearchQuery
from flypoints.providers.base import AuthError, ProviderError

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

    def search(self, origin, destination, date_from, date_to, cabin):
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
                date_to=date(2026, 12, 4), cabin=Cabin.J)
    return SearchQuery(**{**base, **kw})


def run(q, providers, cache, **kw):
    kw.setdefault("include_cash", False)
    return engine.search(q, award_providers=providers, cache=cache, today=TODAY, **kw)


def test_region_and_window_expansion(cache):
    p = FakeAward()
    q = query(origins=["SJU"], destinations=["JPN"], date_to=date(2026, 12, 9))  # 9 days -> 3 windows
    r = run(q, [p], cache)
    assert r.searches_planned == 9 and r.searches_run == 9
    assert sorted({c[2:] for c in p.calls}) == [
        (date(2026, 12, 1), date(2026, 12, 4)),
        (date(2026, 12, 5), date(2026, 12, 8)),
        (date(2026, 12, 9), date(2026, 12, 9)),
    ]
    assert {c[1] for c in p.calls} == {"HND", "NRT", "KIX"}


def test_cap_skips_uncached_live_searches_in_date_order(cache):
    p = FakeAward()
    r = run(query(date_to=date(2026, 12, 12), max_searches=2), [p], cache)
    assert (r.searches_planned, r.searches_run, r.searches_skipped) == (3, 2, 1)
    assert sorted(c[2] for c in p.calls) == [date(2026, 12, 1), date(2026, 12, 5)]


def test_chart_searches_do_not_count_toward_cap(cache):
    live, charts = FakeAward(), FakeAward(name="charts", window=366)
    r = run(query(max_searches=1), [live, charts], cache)
    assert r.searches_run == 2 and r.searches_skipped == 0


def test_cache_hit_on_second_run_and_cached_do_not_count_toward_cap(cache):
    p = FakeAward(offers=[offer()])
    run(query(), [p], cache)
    r = run(query(max_searches=0), [p], cache)
    assert len(p.calls) == 1
    assert (r.searches_cached, r.searches_run, r.searches_skipped) == (1, 0, 0)
    assert len(r.options) == 1


def test_refresh_bypasses_cache(cache):
    p = FakeAward(offers=[offer()])
    run(query(), [p], cache)
    r = run(query(), [p], cache, refresh=True)
    assert len(p.calls) == 2 and r.searches_cached == 0


def test_progress_called_per_search(cache):
    seen = []
    run(query(date_to=date(2026, 12, 8)), [FakeAward()], cache, progress=lambda d, t, m: seen.append((d, t)))
    assert sorted(seen) == [(1, 2), (2, 2)]


def test_cabin_and_reachability_filter(cache):
    p = FakeAward(offers=[offer("AC"), offer("AC", cabin=Cabin.Y, miles=1), offer("AA", miles=1)])
    r = run(query(), [p], cache)
    assert [o.offer.program_code for o in r.options] == ["AC"]


def test_program_filter_resolves_aliases(cache):
    p = FakeAward(offers=[offer("KL"), offer("AC")])
    r = run(query(programs=["af"]), [p], cache)
    assert [o.offer.program_code for o in r.options] == ["KL"]


def test_dedupe_keeps_lowest_miles_then_taxes(cache):
    p = FakeAward(offers=[offer(miles=70000), offer(miles=60000, taxes=80), offer(miles=60000, taxes=40)])
    r = run(query(), [p], cache)
    assert [(o.offer.miles, o.offer.taxes_usd) for o in r.options] == [(60000, 40)]


def test_estimate_dropped_when_live_offer_exists_for_same_program_route(cache):
    live = FakeAward(offers=[offer("AC", miles=70000)])
    charts = FakeAward(name="charts", window=366,
                       offers=[offer("AC", miles=55000, estimated=True), offer("KL", miles=60000, estimated=True)])
    r = run(query(), [live, charts], cache)
    assert sorted((o.offer.program_code, o.offer.estimated) for o in r.options) == [("AC", False), ("KL", True)]


def test_include_estimates_false_drops_chart_provider(cache):
    charts = FakeAward(name="charts", window=366, offers=[offer("KL", estimated=True)])
    r = run(query(), [FakeAward(), charts], cache, include_estimates=False)
    assert charts.calls == [] and r.options == []


def test_auth_error_aborts(cache):
    p = FakeAward(error=AuthError("token expired"))
    with pytest.raises(AuthError, match="token expired"):
        run(query(date_to=date(2026, 12, 20)), [p], cache)


def test_other_provider_error_recorded_and_run_continues(cache):
    bad = FakeAward(name="bad", error=ProviderError("boom"))
    good = FakeAward(offers=[offer()])
    r = run(query(), [bad, good], cache)
    assert len(r.errors) == 1 and "boom" in r.errors[0] and "bad SJU-MAD" in r.errors[0]
    assert r.searches_run == 2 and len(r.options) == 1
    run(query(), [bad], cache)  # failures are not cached
    assert len(bad.calls) == 2


def test_ranking_by_bank_points(cache):
    p = FakeAward(offers=[offer("AC", miles=90000), offer("KL", miles=50000), offer("BA", miles=70000)])
    r = run(query(), [p], cache)
    assert [o.offer.program_code for o in r.options] == ["KL", "BA", "AC"]


def test_cash_attached_cheapest_fare_and_cpp(cache):
    p = FakeAward(offers=[offer("AC", miles=60000, taxes=50)])
    cash = FakeCash(price=3050.0)
    r = run(query(), [p], cache, cash_provider=cash, include_cash=True)
    o = r.options[0]
    assert o.cash_fare.price_usd == 3050.0
    assert o.cpp == pytest.approx((3050 - 50) / 60000 * 100)
    assert o.portal_points is not None
    r2 = run(query(), [p], cache, cash_provider=cash, include_cash=True)  # cash cached
    assert len(cash.calls) == 1 and r2.options[0].cash_fare is not None


def test_cash_lookup_cap_and_dedup(cache):
    offers = [offer("AC", day=date(2026, 12, d), miles=60000 + d) for d in (1, 2, 3, 4)]
    offers.append(offer("KL", day=date(2026, 12, 1), miles=70000))  # same route/date as first: one lookup
    cash = FakeCash()
    r = run(query(), [FakeAward(offers=offers)], cache, cash_provider=cash, include_cash=True, max_cash_lookups=2)
    assert len(cash.calls) == 2
    assert sum(o.cash_fare is not None for o in r.options) == 3  # 2 dates, KL shares the first date


def test_cash_error_recorded_not_fatal(cache):
    p = FakeAward(offers=[offer()])
    r = run(query(), [p], cache, cash_provider=FakeCash(error=ProviderError("nope")), include_cash=True)
    assert len(r.options) == 1 and r.options[0].cash_fare is None
    assert any("nope" in e for e in r.errors)


def test_cash_can_reorder_ties_by_cpp(cache):
    # equal bank points and taxes: higher cpp ranks first once cash is known
    p = FakeAward(offers=[offer("AC", miles=60000, day=date(2026, 12, 1)),
                          offer("AC", miles=60000, day=date(2026, 12, 2))])

    class PerDay(FakeCash):
        def fares(self, origin, destination, day, cabin):
            self.price = 5000.0 if day.day == 2 else 3000.0
            return super().fares(origin, destination, day, cabin)

    r = run(query(), [p], cache, cash_provider=PerDay(), include_cash=True)
    assert [o.offer.date.day for o in r.options] == [2, 1]


def test_estimator_import_failure_warns(cache, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "flypoints.providers.charts", None)
    warnings = []
    providers = engine._default_providers(cache, True, warnings)
    assert [p.name for p in providers] == ["pointsyeah"] and "estimates skipped" in warnings[0]
