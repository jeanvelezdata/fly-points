"""Core pydantic models shared by every layer (providers, engine, CLI, MCP, web)."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


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
CABIN_ORDER = {Cabin.Y: 0, Cabin.W: 1, Cabin.J: 2, Cabin.F: 3}  # low to high


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
    duration_min: int | None = None  # flight time in minutes, excluding layover; None = unknown


class OptionFilters(BaseModel):
    """Result filters, all optional; the default means no filtering. Applied by the engine
    (``engine.filter_options``) after pricing."""

    direct_only: bool = False
    max_stops: int | None = None
    carriers: list[str] | None = None  # operating carrier codes; every segment must match
    days_of_week: list[int] | None = None  # 0=Mon..6=Sun, on the offer's date
    max_taxes_usd: float | None = None
    max_points: int | None = None  # on best_bank_points
    min_seats: int | None = None  # unknown seat counts pass
    banks: list[Bank] | None = None  # only offers reachable from one of these banks
    min_cabin_pct: int | None = None  # min % flown in the offer's cabin; unknown passes

    @field_validator("carriers")
    @classmethod
    def _carriers_upper(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [c.strip().upper() for c in v]

    @field_validator("days_of_week")
    @classmethod
    def _days_valid(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(not 0 <= d <= 6 for d in v):
            raise ValueError("days_of_week values must be 0 (Mon) to 6 (Sun)")
        return v


class SearchQuery(OptionFilters):
    """origins/destinations hold IATA codes or region names (expanded later by the engine)."""

    origins: list[str]
    destinations: list[str]
    date_from: date
    date_to: date
    cabins: list[Cabin]  # non-empty, deduped, order kept
    adults: int = 1
    max_searches: int = 40
    programs: list[str] | None = None  # program-code filter

    @model_validator(mode="before")
    @classmethod
    def _legacy_cabin(cls, data):
        """Old callers and saved searches pass a single ``cabin``."""
        if isinstance(data, dict) and "cabin" in data and "cabins" not in data:
            data = {**data, "cabins": [data["cabin"]]}
            del data["cabin"]
        return data

    @field_validator("cabins")
    @classmethod
    def _cabins_nonempty(cls, v: list[Cabin]) -> list[Cabin]:
        v = list(dict.fromkeys(v))
        if not v:
            raise ValueError("cabins must not be empty")
        return v


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
    #: % (0-100) of the journey flown in a cabin below ``cabin``; 0 = pure, None = unknown.
    mixed_cabin_pct: int | None = None

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
