"""Cash-fare provider composition: fast-flights first, SerpApi as fallback when a key is set."""
from __future__ import annotations

from datetime import date

from flypoints.models import Cabin, CashFare

from .base import CashProvider, ProviderError
from .cash_fastflights import FastFlightsProvider
from .cash_serpapi import SerpApiProvider


class FallbackCashProvider:
    """Tries each provider in order; returns the first non-empty result."""

    def __init__(self, providers: list[CashProvider]):
        self.providers = providers
        self.name = "+".join(p.name for p in providers)

    def fares(self, origin: str, destination: str, day: date, cabin: Cabin) -> list[CashFare]:
        last_error: ProviderError | None = None
        answered = False  # at least one provider responded (possibly with no fares)
        for p in self.providers:
            try:
                result = p.fares(origin, destination, day, cabin)
            except ProviderError as e:
                last_error = e
                continue
            if result:
                return result
            answered = True
        if last_error is not None and not answered:
            raise last_error
        return []


def default_cash_provider() -> CashProvider:
    providers: list[CashProvider] = [FastFlightsProvider()]
    if SerpApiProvider.available():
        providers.append(SerpApiProvider())
    return providers[0] if len(providers) == 1 else FallbackCashProvider(providers)
