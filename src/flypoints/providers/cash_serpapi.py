"""Cash fares from SerpApi's Google Flights engine (free tier: 250 searches/month)."""
from __future__ import annotations

import os
from datetime import date, datetime

import httpx

from flypoints.models import Cabin, CashFare

from .base import ProviderError

URL = "https://serpapi.com/search.json"
ENV_KEY = "SERPAPI_API_KEY"
_TRAVEL_CLASS = {Cabin.Y: 1, Cabin.W: 2, Cabin.J: 3, Cabin.F: 4}


def parse_response(
    data: dict, origin: str, destination: str, day: date, cabin: Cabin
) -> list[CashFare]:
    """Parse a SerpApi google_flights payload (best_flights + other_flights), cheapest first."""
    fares = []
    for itin in data.get("best_flights", []) + data.get("other_flights", []):
        if "price" not in itin:
            continue
        legs = itin.get("flights", [])
        airlines = []
        for leg in legs:
            if leg.get("airline") and leg["airline"] not in airlines:
                airlines.append(leg["airline"])
        fares.append(
            CashFare(
                provider=SerpApiProvider.name,
                origin=origin,
                destination=destination,
                date=day,
                cabin=cabin,
                price_usd=float(itin["price"]),
                airlines=airlines,
                flight_numbers=[leg["flight_number"] for leg in legs if leg.get("flight_number")],
                stops=max(len(legs) - 1, 0),
                fetched_at=datetime.now(),
            )
        )
    return sorted(fares, key=lambda f: f.price_usd)


class SerpApiProvider:
    name = "serpapi"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self._key = api_key or os.environ.get(ENV_KEY)
        if not self._key:
            raise ProviderError(f"SerpApi needs an API key: set the {ENV_KEY} environment variable.")
        self._client = client or httpx.Client(timeout=30)

    @classmethod
    def available(cls) -> bool:
        return bool(os.environ.get(ENV_KEY))

    def fares(self, origin: str, destination: str, day: date, cabin: Cabin) -> list[CashFare]:
        params = {
            "engine": "google_flights",
            "type": 2,
            "travel_class": _TRAVEL_CLASS[cabin],
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": day.isoformat(),
            "currency": "USD",
            "hl": "en",
            "api_key": self._key,
        }
        try:
            resp = self._client.get(URL, params=params)
            data = resp.json() if resp.content else {}
        except (httpx.HTTPError, ValueError):
            # Deliberately omit the exception text: httpx errors can embed the request URL (and key).
            raise ProviderError("SerpApi request failed (network error or invalid response)") from None
        if resp.status_code != 200 or "error" in data:
            err = data.get("error", f"HTTP {resp.status_code}")
            if "hasn't returned any results" in err:
                return []
            raise ProviderError(f"SerpApi error: {err}")
        return parse_response(data, origin, destination, day, cabin)
