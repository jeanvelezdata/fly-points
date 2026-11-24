from datetime import date, datetime, timedelta

import pytest

from flypoints import views
from flypoints.config import load_bank_rules, load_programs
from flypoints.models import AwardOffer, Bank, Cabin, Segment
from flypoints.pricing import build_ranked_option

NOW = datetime(2026, 10, 19, 12, 0)


def make(code="AC", cabin=Cabin.J, miles=60000, seats=2, via=(), estimated=False, mixed=0, day=date(2026, 12, 2),
         taxes=50.0, fetched_at=NOW):
    stops = ["SJU", *via, "MAD"]
    segments = [] if estimated else [
        Segment(flight_number="AC1", origin=a, destination=b, depart=NOW, arrive=NOW) for a, b in zip(stops, stops[1:])
    ]
    o = AwardOffer(provider="t", program_code=code, program_name=code, origin="SJU", destination="MAD", date=day,
                   cabin=cabin, miles=miles, taxes_usd=taxes, seats=seats, segments=segments, estimated=estimated,
                   mixed_cabin_pct=mixed, fetched_at=fetched_at)
    return build_ranked_option(o, None, load_programs(), load_bank_rules(), [])


@pytest.mark.parametrize("delta,text", [
    (timedelta(seconds=5), "just now"),
    (timedelta(minutes=5, seconds=30), "5m ago"),
    (timedelta(minutes=59), "59m ago"),
    (timedelta(hours=3, minutes=10), "3h ago"),
    (timedelta(hours=47), "47h ago"),
    (timedelta(days=2), "2d ago"),
    (timedelta(days=10), "10d ago"),
    (timedelta(seconds=-30), "just now"),  # clock skew
])
def test_relative_age(delta, text):
    assert views.relative_age(NOW - delta, now=NOW) == text


def test_seen_label_dashes_for_estimates():
    live, est = make().offer, make(estimated=True).offer
    assert views.seen_label(live, now=NOW + timedelta(hours=3)) == "3h ago"
    assert views.seen_label(est, now=NOW + timedelta(hours=3)) == "-"


def test_mixed_cabin_marker():
    assert views.mixed_cabin_marker(make(mixed=0).offer) == ("", "")
    assert views.mixed_cabin_marker(make(mixed=28).offer) == ("*", "72% J")
    assert views.mixed_cabin_marker(make(mixed=None).offer) == ("?", "cabin mix unknown")
    assert views.mixed_cabin_marker(make(mixed=None, estimated=True).offer) == ("", "")


def test_option_route():
    assert views.option_route(make(via=["EWR"])) == "SJU-EWR-MAD"
    assert views.option_route(make(estimated=True)) == "SJU-MAD"


def overnight():
    """SJU 13:00 -> EWR 16:17, EWR 20:00 -> MAD 09:30 next day (the fixture's UA2461/UA51)."""
    o = make(via=["EWR"])
    first, second = o.offer.segments
    first.flight_number, first.depart, first.arrive = "UA2461", datetime(2026, 12, 2, 13, 0), datetime(2026, 12, 2, 16, 17)
    second.flight_number, second.depart, second.arrive = "UA51", datetime(2026, 12, 2, 20, 0), datetime(2026, 12, 3, 9, 30)
    return o


def test_option_times():
    assert views.option_times(overnight()) == ("13:00", "09:30+1")
    assert views.option_times(make()) == ("12:00", "12:00")  # same day: no marker
    assert views.option_times(make(estimated=True)) is None


def test_segment_lines():
    assert views.segment_lines(overnight()) == [
        "UA2461 SJU 2026-12-02 13:00 -> EWR 16:17",
        "UA51 EWR 2026-12-02 20:00 -> MAD 09:30+1",
    ]
    assert views.segment_lines(make(estimated=True)) == []


def test_compare_grid_groups_by_program_route_date_and_picks_best_per_cabin():
    options = [
        make("AC", Cabin.J, 70000, via=["EWR"]),
        make("AC", Cabin.J, 60000, via=["EWR"]),  # cheaper J on the same flights wins
        make("AC", Cabin.Y, 20000, via=["EWR"]),
        make("AC", Cabin.J, 55000),  # different route: own row
        make("AV", Cabin.J, 51100, seats=None),
        make("AC", Cabin.J, 62000, via=["EWR"], day=date(2026, 12, 3)),  # different date: own row
    ]
    rows = views.compare_grid(options)
    assert [(r.program_code, r.route, r.date.day) for r in rows] == [
        ("AC", "SJU-EWR-MAD", 2), ("AC", "SJU-MAD", 2), ("AV", "SJU-MAD", 2), ("AC", "SJU-EWR-MAD", 3),
    ]
    first = rows[0]
    assert set(first.cells) == {Cabin.J, Cabin.Y}
    j = first.cells[Cabin.J]
    assert j.miles == 60000 and j.points == j.option.best_bank_points and j.bank in ("Chase", "Capital One")
    assert j.seats == 2 and not j.estimated
    assert rows[2].cells[Cabin.J].seats is None


def test_compare_grid_ties_and_missing_points():
    cheap_taxes = make("AC", Cabin.J, 60000, taxes=10.0)
    pricey_taxes = make("AC", Cabin.J, 60000, taxes=90.0)
    row = views.compare_grid([pricey_taxes, cheap_taxes])[0]
    assert row.cells[Cabin.J].option is cheap_taxes
    assert views.compare_grid([]) == []


def test_parsers():
    assert views.flatten_csv(["Y,J", " F "]) == ["Y", "J", "F"] and views.flatten_csv("a, b") == ["a", "b"]
    assert views.flatten_csv(None) == []
    assert views.parse_cabins(["y,j", "Business"]) == [Cabin.Y, Cabin.J, Cabin.J]
    assert views.parse_days("mon,Friday") == [0, 4]
    assert views.parse_banks(["chase", "Capital One,capital-one"]) == [Bank.chase, Bank.capital_one, Bank.capital_one]
    for fn, bad in ((views.parse_cabins, "Z"), (views.parse_days, "funday"), (views.parse_banks, "amex")):
        with pytest.raises(ValueError):
            fn(bad)
