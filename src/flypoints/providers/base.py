"""Provider interfaces shared by every data source.

Providers are synchronous and stateless with respect to caching: the engine handles
fan-out, caching and rate limiting. A provider only translates one request into models.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from flypoints.models import AwardOffer, Cabin, CashFare


class ProviderError(Exception):
    """A provider failed in a way the user may need to act on (message is user-facing)."""


class AuthError(ProviderError):
    """Credentials are missing or expired; message says how to fix it."""


@runtime_checkable
class AwardProvider(Protocol):
    name: str
    #: Longest date_from..date_to span (inclusive, in days) one search() call accepts.
    max_window_days: int

    def search(
        self, origin: str, destination: str, date_from: date, date_to: date, cabin: Cabin
    ) -> list[AwardOffer]:
        """Award offers for one airport pair over an inclusive date window.

        May return offers in other cabins too; the engine filters. Raises ProviderError.
        """
        ...


@runtime_checkable
class CashProvider(Protocol):
    name: str

    def fares(self, origin: str, destination: str, day: date, cabin: Cabin) -> list[CashFare]:
        """Cash fares for one airport pair, one date, one cabin; cheapest first. Raises ProviderError."""
        ...
