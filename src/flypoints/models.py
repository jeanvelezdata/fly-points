"""Core pydantic models shared by every layer (providers, engine, CLI, MCP, web)."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Cabin(str, Enum):
    """Cabin class: Y economy, W premium economy, J business, F first."""

    Y = "Y"
    W = "W"
    J = "J"
    F = "F"

    @classmethod
    def from_provider(cls, value: str) -> "Cabin":
        """Map "Economy", "Premium Economy", "Business", "First" (case-insensitive;
        "premium-economy"/"premium_economy" also accepted) or a Y/W/J/F code to a Cabin.
        Raises ValueError for anything else."""
        key = value.strip().lower().replace("-", " ").replace("_", " ")
        try:
            return _CABIN_BY_NAME[key]
        except KeyError:
            raise ValueError(f"Unknown cabin: {value!r}") from None

    def to_provider(self) -> str:
        """Human/provider name: "Economy", "Premium Economy", "Business", "First"."""
        return _CABIN_NAMES[self]


_CABIN_NAMES = {
    Cabin.Y: "Economy",
    Cabin.W: "Premium Economy",
    Cabin.J: "Business",
    Cabin.F: "First",
}
_CABIN_BY_NAME = {name.lower(): c for c, name in _CABIN_NAMES.items()}
_CABIN_BY_NAME.update({c.value.lower(): c for c in Cabin})


class Bank(str, Enum):
    chase = "chase"
    capital_one = "capital_one"

    @property
    def label(self) -> str:
        return "Chase" if self is Bank.chase else "Capital One"


class Segment(BaseModel):
    flight_number: str
    origin: str
    destination: str
    depart: datetime
    arrive: datetime
    aircraft: str | None = None
    cabin: Cabin | None = None


class SearchQuery(BaseModel):
    """origins/destinations hold IATA codes or region names (expanded later by the engine)."""

    origins: list[str]
    destinations: list[str]
    date_from: date
    date_to: date
    cabin: Cabin
    adults: int = 1
    max_searches: int = 40
    programs: list[str] | None = None  # program-code filter


class AwardOffer(BaseModel):
    provider: str
    program_code: str  # PointsYeah 2-letter code, e.g. "AC"
    program_name: str
    origin: str
    destination: str
    date: date
    cabin: Cabin
    miles: int
    taxes_usd: float
    seats: int | None = None  # None = unknown
    segments: list[Segment] = Field(default_factory=list)
    booking_url: str | None = None
    estimated: bool = False
    fetched_at: datetime

    @property
    def key(self) -> tuple:
        """Dedupe key: (program, date, cabin, flight numbers)."""
        return (
            self.program_code,
            self.date,
            self.cabin,
            tuple(s.flight_number for s in self.segments),
        )


class CashFare(BaseModel):
    provider: str
    origin: str
    destination: str
    date: date
    cabin: Cabin
    price_usd: float
    airlines: list[str] = Field(default_factory=list)
    flight_numbers: list[str] = Field(default_factory=list)
    stops: int = 0
    fetched_at: datetime


class BankOption(BaseModel):
    bank: Bank
    ratio: float  # partner miles per bank point
    bonus: float  # fraction, e.g. 0.3
    bank_points: int


class RankedOption(BaseModel):
    offer: AwardOffer
    bank_options: list[BankOption] = Field(default_factory=list)  # only banks reaching the program
    best_bank: Bank | None = None
    best_bank_points: int | None = None
    cash_fare: CashFare | None = None
    cpp: float | None = None
    portal_points: dict[Bank, int] | None = None  # cash fare cost via each bank's portal
    note: str | None = None
