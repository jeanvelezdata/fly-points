"""Cash fares from Google Flights via the fast-flights library (free, reverse-engineered, fragile)."""
from __future__ import annotations

from datetime import date, datetime

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from fast_flights.fetcher import URL
from fast_flights.integrations.base import FetchIntegration
from fast_flights.querying import Query
from primp import Client

from flypoints.models import Cabin, CashFare

from .base import ProviderError

_SEAT = {Cabin.Y: "economy", Cabin.W: "premium-economy", Cabin.J: "business", Cabin.F: "first"}

# From an EU IP (the deployment VM is in Finland) Google answers with its cookie-consent page and
# fast-flights can't parse it. SOCS=CAI is the "reject all" consent state; it was enough to get
# real results on 2026-11-24, so no "accept" consent is sent.
CONSENT_COOKIES = {"SOCS": "CAI"}


class _ConsentFetch(FetchIntegration):
    """fast-flights' default fetcher (same client settings) plus the consent cookie."""

    def fetch_html(self, q: Query | str, /) -> str:
        client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
        params = q.params() if isinstance(q, Query) else {"q": q}
        return client.get(URL, params=params, cookies=CONSENT_COOKIES).text


def to_cash_fare(f, origin: str, destination: str, day: date, cabin: Cabin) -> CashFare:
    """Convert one fast-flights ``Flights`` result to a CashFare.

    fast-flights 3.1.0 does not expose flight numbers, so ``flight_numbers`` is empty.
    """
    return CashFare(
        provider=FastFlightsProvider.name,
        origin=origin,
        destination=destination,
        date=day,
        cabin=cabin,
        price_usd=float(f.price),
        airlines=list(f.airlines),
        stops=max(len(f.flights) - 1, 0),
        fetched_at=datetime.now(),
    )


class FastFlightsProvider:
    name = "fast-flights"

    def fares(self, origin: str, destination: str, day: date, cabin: Cabin) -> list[CashFare]:
        query = create_query(
            flights=[FlightQuery(date=day.isoformat(), from_airport=origin, to_airport=destination)],
            seat=_SEAT[cabin],
            trip="one-way",
            passengers=Passengers(adults=1),
            currency="USD",
            language="en",
        )
        try:
            results = list(get_flights(query, integration=_ConsentFetch()))
        except FlightsNotFound:
            return []
        except Exception as e:  # library is a scraper: parse errors, network errors, etc.
            raise ProviderError(f"fast-flights failed for {origin}->{destination} {day}: {e}") from e
        fares = [to_cash_fare(f, origin, destination, day, cabin) for f in results]
        return sorted(fares, key=lambda x: x.price_usd)
