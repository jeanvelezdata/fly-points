import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from flypoints.models import Cabin, CashFare
from flypoints.providers import cash_fastflights
from flypoints.providers.base import CashProvider, ProviderError
from flypoints.providers.cash import FallbackCashProvider, default_cash_provider
from flypoints.providers.cash_fastflights import FastFlightsProvider, to_cash_fare
from flypoints.providers.cash_serpapi import URL, SerpApiProvider

DAY = date(2026, 12, 2)
FIXTURE = Path(__file__).parent / "fixtures" / "serpapi_sample.json"


def _ff(price, airlines, n_legs):
    return SimpleNamespace(price=price, airlines=airlines, flights=[object()] * n_legs)


def test_to_cash_fare_conversion():
    fare = to_cash_fare(_ff(4766, ["Iberia"], 1), "SJU", "MAD", DAY, Cabin.J)
    assert fare.provider == "fast-flights"
    assert (fare.price_usd, fare.stops, fare.airlines, fare.flight_numbers) == (4766.0, 0, ["Iberia"], [])
    assert fare.cabin is Cabin.J and fare.date == DAY
    assert to_cash_fare(_ff(1921, ["Avianca"], 2), "SJU", "MAD", DAY, Cabin.J).stops == 1


def _patch_ff(monkeypatch, results=None, exc=None):
    seen = {}

    def fake_get_flights(q):
        seen["q"] = q
        if exc:
            raise exc
        return results

    monkeypatch.setattr(cash_fastflights, "get_flights", fake_get_flights)
    return seen


@pytest.mark.parametrize(
    "cabin,seat",
    [(Cabin.Y, "economy"), (Cabin.W, "premium-economy"), (Cabin.J, "business"), (Cabin.F, "first")],
)
def test_fastflights_cabin_mapping_and_sorting(monkeypatch, cabin, seat):
    seen = _patch_ff(monkeypatch, [_ff(900, ["B"], 1), _ff(300, ["A"], 0)])
    fares = FastFlightsProvider().fares("SJU", "MAD", DAY, cabin)
    assert [f.price_usd for f in fares] == [300.0, 900.0]
    assert cash_fastflights._SEAT[cabin] == seat
    assert seen["q"].get_seat_type() == seat


def test_fastflights_not_found_returns_empty(monkeypatch):
    _patch_ff(monkeypatch, exc=cash_fastflights.FlightsNotFound())
    assert FastFlightsProvider().fares("SJU", "MAD", DAY, Cabin.Y) == []


def test_fastflights_other_errors_wrapped(monkeypatch):
    _patch_ff(monkeypatch, exc=RuntimeError("boom"))
    with pytest.raises(ProviderError, match="boom"):
        FastFlightsProvider().fares("SJU", "MAD", DAY, Cabin.Y)


def test_serpapi_missing_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert not SerpApiProvider.available()
    with pytest.raises(ProviderError, match="SERPAPI_API_KEY"):
        SerpApiProvider()
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    assert SerpApiProvider.available()


@respx.mock
def test_serpapi_parsing_and_params():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
    fares = SerpApiProvider(api_key="secret").fares("SJU", "MAD", DAY, Cabin.J)
    assert [f.price_usd for f in fares] == [250.0, 302.0, 410.0]
    assert fares[0].stops == 1 and fares[0].airlines == ["Avianca"]
    assert fares[0].flight_numbers == ["AV 245", "AV 12"]
    assert fares[1].flight_numbers == ["IB 6124"] and fares[1].stops == 0
    p = route.calls[0].request.url.params
    assert p["engine"] == "google_flights" and p["type"] == "2" and p["travel_class"] == "3"
    assert (p["departure_id"], p["arrival_id"], p["outbound_date"]) == ("SJU", "MAD", "2026-12-02")
    assert p["currency"] == "USD" and p["hl"] == "en"


@respx.mock
def test_serpapi_errors():
    prov = SerpApiProvider(api_key="secret")
    msg = "Google Flights hasn't returned any results for this query."
    respx.get(URL).mock(return_value=httpx.Response(200, json={"error": msg}))
    assert prov.fares("SJU", "MAD", DAY, Cabin.Y) == []
    respx.get(URL).mock(return_value=httpx.Response(401, json={"error": "Invalid API key."}))
    with pytest.raises(ProviderError, match="Invalid API key"):
        prov.fares("SJU", "MAD", DAY, Cabin.Y)
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError) as ei:
        prov.fares("SJU", "MAD", DAY, Cabin.Y)
    assert "secret" not in str(ei.value)


class Stub:
    def __init__(self, name, result=None, exc=None):
        self.name, self.result, self.exc, self.calls = name, result, exc, 0

    def fares(self, origin, destination, day, cabin):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _fare():
    return CashFare(provider="x", origin="SJU", destination="MAD", date=DAY, cabin=Cabin.Y,
                    price_usd=1.0, fetched_at=datetime.now())


def test_fallback_order_and_name():
    a, b = Stub("a", [_fare()]), Stub("b", [_fare()])
    fb = FallbackCashProvider([a, b])
    assert fb.name == "a+b" and isinstance(fb, CashProvider)
    assert fb.fares("SJU", "MAD", DAY, Cabin.Y) and (a.calls, b.calls) == (1, 0)


def test_fallback_skips_empty_and_error():
    a, b, c = Stub("a", exc=ProviderError("a down")), Stub("b", []), Stub("c", [_fare()])
    assert FallbackCashProvider([a, b, c]).fares("SJU", "MAD", DAY, Cabin.Y)
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)


def test_fallback_all_raise_gives_last_error():
    fb = FallbackCashProvider([Stub("a", exc=ProviderError("first")), Stub("b", exc=ProviderError("last"))])
    with pytest.raises(ProviderError, match="last"):
        fb.fares("SJU", "MAD", DAY, Cabin.Y)


def test_fallback_error_then_empty_is_empty():
    fb = FallbackCashProvider([Stub("a", exc=ProviderError("x")), Stub("b", [])])
    assert fb.fares("SJU", "MAD", DAY, Cabin.Y) == []


def test_default_provider(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert default_cash_provider().name == "fast-flights"
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    assert default_cash_provider().name == "fast-flights+serpapi"


@pytest.mark.live
@pytest.mark.parametrize("cabin", [Cabin.Y, Cabin.J])
def test_fastflights_live(cabin):
    fares = FastFlightsProvider().fares("SJU", "MAD", date(2026, 12, 2), cabin)
    assert fares and fares == sorted(fares, key=lambda f: f.price_usd)
    for f in fares[:5]:
        print(cabin.value, f.price_usd, f.stops, f.airlines)
