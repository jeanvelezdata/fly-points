from datetime import date

import pytest

from flypoints import config
from flypoints.models import Bank, Cabin
from flypoints.providers import charts
from flypoints.providers.charts import ChartEstimator, distance_miles, estimate


def test_airports_cover_regions():
    airports = charts._airports()
    for region, codes in config.load_regions().items():
        assert set(codes) <= set(airports), region


def test_distance_statute_miles():
    # SJU-MAD is ~6,380 km great-circle = ~3,964 statute miles.
    assert distance_miles("SJU", "MAD") == pytest.approx(3964, abs=40)
    assert distance_miles("SJU", "MAD") * 1.609344 == pytest.approx(6380, abs=60)
    assert distance_miles("JFK", "LHR") == pytest.approx(3443, abs=30)
    assert distance_miles("jfk", "LHR") == distance_miles("LHR", "JFK")
    assert distance_miles("SJU", "XXX") is None


# Aeroplan partner chart, awardtravelfinder.com/award-charts/aeroplan (read 2026-10-19); the
# NA-Atlantic, NA-Pacific, Atlantic-Pacific and intra-Atlantic rows also match
# princeoftravel.com/news/aeroplan-flight-reward-chart-june-2026/ (post-June-2026 column).
@pytest.mark.parametrize(
    "origin, dest, cabin, expected",
    [
        ("JFK", "LHR", "J", 60_000),  # NA-Atlantic 0-4,000 mi (3,443)
        ("SJU", "MAD", "Y", 32_500),  # NA-Atlantic 0-4,000 mi (3,964: close to the 4,000 edge)
        ("SJU", "MAD", "W", 50_000),
        ("IST", "JFK", "F", 120_000),  # NA-Atlantic 4,001-6,000 mi (5,000)
        ("JFK", "NRT", "J", 85_000),  # NA-Pacific 5,001-7,500 mi (6,729)
        ("MIA", "GRU", "J", 50_000),  # NA-South America 2,501-4,500 mi (4,084)
        ("SJU", "BOG", "Y", 20_000),  # NA-South America 0-2,500 mi
        ("LHR", "SIN", "J", 92_500),  # Atlantic-Pacific 5,001-7,000 mi (6,761)
        ("MAD", "IST", "J", 22_500),  # intra-Atlantic 1,001-2,000 mi
        ("SJU", "MIA", "J", 20_000),  # intra-North America 501-1,500 mi
    ],
)
def test_aeroplan(origin, dest, cabin, expected):
    assert estimate("AC", origin, dest, cabin) == expected


# Turkish Miles&Smiles Star Alliance partner chart, awardfares.com/blog/miles-and-smiles-guide
# (verified there 2026-07-14); Europe 2 row also in meilenoptimieren.com (FRA-NYC business 90,000).
@pytest.mark.parametrize(
    "origin, dest, cabin, expected",
    [
        ("FRA", "JFK", "J", 90_000),  # Europe 2 - North America
        ("MAD", "SJU", "Y", 55_000),  # PR assumed North America (see TK.yaml caveat)
        ("JFK", "IST", "J", 65_000),  # Turkiye - North America
        ("MAD", "IST", "F", 35_000),  # Turkiye - Europe 2
        ("ATH", "JFK", "J", 85_000),  # Europe 1 - North America
        ("JFK", "YYZ", "J", 40_000),  # cross-border within North America
        ("SJU", "BQN", "J", 22_500),  # same country (domestic partner award)
        ("MIA", "GRU", "J", 140_000),  # North America - South America
        ("MAD", "HND", "J", 90_000),  # Europe 2 - Far East
    ],
)
def test_turkish(origin, dest, cabin, expected):
    assert estimate("TK", origin, dest, cabin) == expected


def test_unknown_airport_and_program():
    assert estimate("AC", "XXX", "MAD", "J") is None
    assert estimate("AC", "SJU", "XXX", "J") is None
    assert estimate("ZZ", "SJU", "MAD", "J") is None


def test_cabin_not_offered():
    assert estimate("TK", "FRA", "JFK", "W") is None
    assert estimate("AC", "SJU", "MIA", "F") is None  # no first on intra-North America


def test_chart_files_valid():
    programs = config.load_programs()
    assert {"AC", "TK"} <= set(charts._charts())
    for code, chart in charts._charts().items():
        assert code in programs
        assert programs[code].banks.keys() & {Bank.chase, Bank.capital_one}
        assert chart["basis"] in ("distance", "zone")
        assert chart["verified"] and chart["source"] and chart["taxes_usd_typical"] >= 0
        zones = set(chart["zones"])
        for entry in chart["prices"]:
            assert set(entry["pair"]) <= zones
            assert ("bands" in entry) == (chart["basis"] == "distance")


def test_search_offers():
    offers = ChartEstimator().search("SJU", "MAD", date(2027, 3, 1), date(2027, 3, 4), Cabin.J)
    by_code = {o.program_code: o for o in offers}
    assert set(by_code) == {"AC", "TK"}
    o = by_code["AC"]
    assert (o.miles, o.estimated, o.provider, o.date) == (60_000, True, "charts", date(2027, 3, 1))
    assert o.seats is None and o.segments == [] and o.booking_url is None
    assert o.program_name == "Air Canada Aeroplan"
    assert ChartEstimator().search("SJU", "XXX", date(2027, 3, 1), date(2027, 3, 1), Cabin.J) == []
