"""Offline award-chart estimator: published saver prices for chart-based programs.

Offers are flagged ``estimated=True``: a chart gives the price, not seat availability.
Chart files live in ``data/charts/`` (schema in its README). Distances are statute miles.
"""
from __future__ import annotations

import functools
from datetime import date, datetime
from math import asin, cos, radians, sin, sqrt

import yaml

from flypoints.config import load_programs
from flypoints.models import AwardOffer, Cabin
from flypoints.paths import DATA_DIR

EARTH_RADIUS_MILES = 3958.8


@functools.cache
def _airports() -> dict:
    return yaml.safe_load((DATA_DIR / "airports.yaml").read_text(encoding="utf-8"))


@functools.cache
def _charts() -> dict[str, dict]:
    out = {}
    for path in sorted((DATA_DIR / "charts").glob("*.yaml")):
        chart = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[chart["program"]] = chart
    return out


def distance_miles(origin: str, destination: str) -> float | None:
    """Great-circle statute miles between two IATA codes; None if either is unknown."""
    a, b = _airports().get(origin.upper()), _airports().get(destination.upper())
    if a is None or b is None:
        return None
    lat1, lon1, lat2, lon2 = (radians(v) for v in (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * asin(sqrt(h))


def _zone(chart: dict, country: str) -> str | None:
    return next((z for z, countries in chart["zones"].items() if country in countries), None)


def estimate(program_code: str, origin: str, destination: str, cabin: Cabin | str) -> int | None:
    """Chart saver price in miles, or None (no chart, unknown airport/zone, or cabin not sold)."""
    chart = _charts().get(program_code.upper())
    a, b = _airports().get(origin.upper()), _airports().get(destination.upper())
    if chart is None or a is None or b is None:
        return None
    cabin = Cabin(cabin).value
    if chart.get("domestic") and a["country"] == b["country"]:
        return chart["domestic"].get(cabin)
    za, zb = _zone(chart, a["country"]), _zone(chart, b["country"])
    if za is None or zb is None:
        return None
    entry = next((e for e in chart["prices"] if sorted(e["pair"]) == sorted([za, zb])), None)
    if entry is None:
        return None
    if chart["basis"] == "zone":
        return entry.get(cabin)
    miles = distance_miles(origin, destination)
    band = next((x for x in entry["bands"] if "up_to" not in x or miles <= x["up_to"]), None)
    return band.get(cabin) if band else None


class ChartEstimator:
    name = "charts"
    max_window_days = 366

    def search(
        self, origin: str, destination: str, date_from: date, date_to: date, cabin: Cabin
    ) -> list[AwardOffer]:
        """One estimated offer per chart program reachable from Chase or Capital One."""
        programs = load_programs()
        offers = []
        for code, chart in _charts().items():
            program = programs.get(code)
            miles = estimate(code, origin, destination, cabin)
            if program is None or not program.banks or miles is None:
                continue
            offers.append(
                AwardOffer(
                    provider=self.name,
                    program_code=code,
                    program_name=program.name,
                    origin=origin.upper(),
                    destination=destination.upper(),
                    date=date_from,
                    cabin=cabin,
                    miles=miles,
                    taxes_usd=float(chart["taxes_usd_typical"]),
                    estimated=True,
                    fetched_at=datetime.now(),
                )
            )
        return offers
