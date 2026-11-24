from datetime import date, datetime, timedelta

import pytest

from flypoints.models import AwardOffer, Cabin, Segment
from flypoints.store import OfferStore

T0 = datetime(2026, 10, 19, 12, 0)
D = date(2026, 12, 1)
D4 = date(2026, 12, 4)


def mk(day=D, code="AC", miles=60000, cabin=Cabin.J, flights=("AC1",), origin="SJU", dest="MAD", seats=2):
    t = datetime(2026, 12, 1)
    return AwardOffer(
        provider="pointsyeah", program_code=code, program_name=code, origin=origin, destination=dest,
        date=day, cabin=cabin, miles=miles, taxes_usd=50.0, seats=seats, fetched_at=T0,
        segments=[Segment(flight_number=f, origin=origin, destination=dest, depart=t, arrive=t) for f in flights],
    )


@pytest.fixture
def store(tmp_path):
    st = OfferStore(tmp_path / "o.sqlite")
    yield st
    st.close()


def rec(store, offers, d_from=D, d_to=D4, cabins=(Cabin.J,), at=T0, provider="pointsyeah"):
    store.record_search(provider, "SJU", "MAD", d_from, d_to, list(cabins), offers, at)


def q(store, cabins=(Cabin.J,), max_age_s=None, now=T0, d_from=D, d_to=D4):
    return store.query(["SJU"], ["MAD"], d_from, d_to, list(cabins), max_age_s, now)


def test_record_and_query_roundtrip(store):
    o = mk()
    rec(store, [o])
    got = q(store)
    assert got == [o] and got[0].segments[0].flight_number == "AC1"
    assert q(store, cabins=[Cabin.Y]) == []
    assert store.query(["BQN"], ["MAD"], D, D, [Cabin.J]) == []


def test_upsert_newer_fetch_replaces_and_duplicates_keep_cheapest(store):
    rec(store, [mk(miles=70000), mk(miles=65000)], at=T0)
    assert [o.miles for o in q(store)] == [65000]
    rec(store, [mk(miles=62000)], at=T0 + timedelta(hours=1))
    assert [o.miles for o in q(store)] == [62000]


def test_rerecording_window_removes_vanished_offers_only_for_that_window_and_cabins(store):
    rec(store, [mk(code="AC"), mk(code="AV", flights=("AV1",)), mk(day=date(2026, 12, 9), code="TK")],
        d_to=date(2026, 12, 9), at=T0)
    rec(store, [mk(cabin=Cabin.Y, code="AA", flights=("AA1",))], cabins=[Cabin.Y], at=T0)
    rec(store, [mk(code="AC")], at=T0 + timedelta(hours=1))  # AV disappeared from Dec 1-4, J
    got = q(store, d_to=date(2026, 12, 9))
    assert sorted(o.program_code for o in got) == ["AC", "TK"]
    assert [o.program_code for o in q(store, cabins=[Cabin.Y])] == ["AA"]  # other cabin untouched


def test_offers_outside_route_window_or_cabins_are_ignored(store):
    rec(store, [mk(day=date(2026, 12, 20)), mk(cabin=Cabin.F), mk(dest="BCN"), mk()])
    assert len(store.query(["SJU"], ["MAD", "BCN"], D, date(2026, 12, 31), list(Cabin))) == 1


def test_query_max_age(store):
    rec(store, [mk()], at=T0)
    assert len(q(store, max_age_s=3600, now=T0 + timedelta(minutes=59))) == 1
    assert q(store, max_age_s=3600, now=T0 + timedelta(minutes=61)) == []


def test_coverage_includes_searched_nothing_and_requires_all_cabins(store):
    assert store.coverage("SJU", "MAD", D, D4, [Cabin.J]) == set()  # never searched
    rec(store, [], d_from=date(2026, 12, 2), d_to=date(2026, 12, 3))  # searched, nothing found
    days = {date(2026, 12, 2), date(2026, 12, 3)}
    assert store.coverage("SJU", "MAD", D, D4, [Cabin.J]) == days
    assert store.coverage("SJU", "MAD", D, D4, [Cabin.J, Cabin.Y]) == set()
    rec(store, [], d_from=date(2026, 12, 3), d_to=D4, cabins=[Cabin.J, Cabin.Y])
    assert store.coverage("SJU", "MAD", D, D4, [Cabin.J, Cabin.Y]) == {date(2026, 12, 3), D4}
    assert store.coverage("SJU", "MAD", D, D4, [Cabin.J]) == days | {D4}


def test_coverage_freshness_and_provider(store):
    rec(store, [], at=T0)
    args = ("SJU", "MAD", D, D4, [Cabin.J])
    assert len(store.coverage(*args, 3600, T0 + timedelta(minutes=30))) == 4
    assert store.coverage(*args, 3600, T0 + timedelta(hours=2)) == set()
    assert len(store.coverage(*args, provider="pointsyeah")) == 4
    assert store.coverage(*args, provider="other") == set()


def test_prune_drops_past_dates_and_old_rows(store):
    rec(store, [mk(day=D)], d_from=D, d_to=D, at=T0)
    rec(store, [mk(day=date(2026, 12, 20), flights=("AC2",))], d_from=date(2026, 12, 20),
        d_to=date(2026, 12, 20), at=T0)
    assert store.prune(date(2026, 12, 2), now=T0 + timedelta(days=1)) == 2  # Dec 1 offer + its search
    assert len(store.query(["SJU"], ["MAD"], D, date(2026, 12, 31), [Cabin.J])) == 1
    assert store.prune(date(2026, 12, 2), now=T0 + timedelta(days=31)) == 2  # remaining rows too old
    assert store.query(["SJU"], ["MAD"], D, date(2026, 12, 31), [Cabin.J]) == []
    assert store.coverage("SJU", "MAD", date(2026, 12, 20), date(2026, 12, 20), [Cabin.J]) == set()
