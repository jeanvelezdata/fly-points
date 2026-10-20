"""Cash fares from Google Flights via the fast-flights library (free, reverse-engineered, fragile)."""
from __future__ import annotations

from datetime import date, datetime

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound

from flypoints.models import Cabin, CashFare

from .base import ProviderError

_SEAT = {Cabin.Y: "economy", Cabin.W: "premium-economy", Cabin.J: "business", Cabin.F: "first"}


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
            results = list(get_flights(query))
        except FlightsNotFound:
            return []
        except Exception as e:  # library is a scraper: parse errors, network errors, etc.
            raise ProviderError(f"fast-flights failed for {origin}->{destination} {day}: {e}") from e
        fares = [to_cash_fare(f, origin, destination, day, cabin) for f in results]
        return sorted(fares, key=lambda x: x.price_usd)
