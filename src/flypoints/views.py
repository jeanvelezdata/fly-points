"""Presentation and input-parsing helpers shared by the CLI, MCP server and web app. Pure functions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from .models import AwardOffer, Bank, Cabin, RankedOption

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- input parsing (raise ValueError with a user-facing message) -----------------------------


def flatten_csv(values: Iterable[str] | str | None) -> list[str]:
    """["Y,J", "F"] or "Y,J" -> ["Y", "J", "F"]; blanks dropped."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [t.strip() for v in values for t in v.split(",") if t.strip()]


def parse_cabins(values: Iterable[str] | str | None) -> list[Cabin]:
    return [Cabin.from_provider(v) for v in flatten_csv(values)]


def parse_days(values: Iterable[str] | str | None) -> list[int]:
    """Day names (mon, Tuesday, ...) to 0=Mon..6=Sun."""
    out = []
    for v in flatten_csv(values):
        try:
            out.append(DAY_NAMES.index(v.lower()[:3]))
        except ValueError:
            raise ValueError(f"Unknown day {v!r}: use mon, tue, wed, thu, fri, sat or sun.") from None
    return out


def parse_banks(values: Iterable[str] | str | None) -> list[Bank]:
    out = []
    for v in flatten_csv(values):
        try:
            out.append(Bank(v.lower().replace(" ", "_").replace("-", "_")))
        except ValueError:
            raise ValueError(f"Unknown bank {v!r}: use chase or capital_one.") from None
    return out


# --- display helpers -------------------------------------------------------------------------


def option_route(o: RankedOption) -> str:
    """"SJU-EWR-MAD" from the segments, or "SJU-MAD" when the offer has no routing."""
    segs = o.offer.segments
    if not segs:
        return f"{o.offer.origin}-{o.offer.destination}"
    return "-".join([segs[0].origin] + [s.destination for s in segs])


def _plus_days(start: datetime, end: datetime) -> str:
    days = (end.date() - start.date()).days
    return f"+{days}" if days > 0 else ""


def option_times(o: RankedOption) -> tuple[str, str] | None:
    """("13:00", "09:30+1"): first departure and last arrival in local airport time, the arrival
    marked +N when it lands N calendar days after departure. None when the offer has no routing."""
    segs = o.offer.segments
    if not segs:
        return None
    dep, arr = segs[0].depart, segs[-1].arrive
    return f"{dep:%H:%M}", f"{arr:%H:%M}{_plus_days(dep, arr)}"


def segment_lines(o: RankedOption) -> list[str]:
    """One line per flight: "UA51 EWR 2026-12-02 20:00 -> MAD 09:30+1" (local airport times)."""
    return [
        f"{s.flight_number} {s.origin} {s.depart:%Y-%m-%d %H:%M} -> {s.destination} {s.arrive:%H:%M}"
        f"{_plus_days(s.depart, s.arrive)}"
        for s in o.offer.segments
    ]


def relative_age(when: datetime, now: datetime | None = None) -> str:
    """"just now", "5m ago", "3h ago", "2d ago"."""
    now = now or datetime.now(when.tzinfo)
    seconds = max((now - when).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def seen_label(offer: AwardOffer, now: datetime | None = None) -> str:
    """Age of the data behind an offer; "-" for award-chart estimates (no search behind them)."""
    return "-" if offer.estimated else relative_age(offer.fetched_at, now)


def mixed_cabin_marker(offer: AwardOffer) -> tuple[str, str]:
    """(marker, detail) for the cabin column: "*" with "72% J" when part of the journey is in a
    lower cabin, "?" when a live offer's cabin mix is unknown, otherwise ("", "")."""
    if offer.estimated:
        return "", ""
    pct = offer.mixed_cabin_pct
    if pct is None:
        return "?", "cabin mix unknown"
    if pct <= 0:
        return "", ""
    return "*", f"{100 - pct}% {offer.cabin.value}"


# --- compare grid ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareCell:
    """The cheapest option (fewest bank points) for one cabin of a compare row."""

    option: RankedOption

    @property
    def points(self) -> int | None:
        return self.option.best_bank_points

    @property
    def miles(self) -> int:
        return self.option.offer.miles

    @property
    def bank(self) -> str | None:
        return self.option.best_bank.label if self.option.best_bank else None

    @property
    def seats(self) -> int | None:
        return self.option.offer.seats

    @property
    def estimated(self) -> bool:
        return self.option.offer.estimated


@dataclass
class CompareRow:
    program_code: str
    route: str
    date: date
    cells: dict[Cabin, CompareCell] = field(default_factory=dict)  # only cabins that have an offer


def _cell_key(opt: RankedOption):
    return (opt.best_bank_points is None, opt.best_bank_points or 0, opt.offer.miles, opt.offer.taxes_usd)


def compare_grid(options: Iterable[RankedOption]) -> list[CompareRow]:
    """One row per (program, route, date) with the best option per cabin, like a quick-compare
    grid. Rows keep the order in which their first option appears."""
    rows: dict[tuple, CompareRow] = {}
    for opt in options:
        a = opt.offer
        row = rows.setdefault(
            (a.program_code, option_route(opt), a.date), CompareRow(a.program_code, option_route(opt), a.date)
        )
        old = row.cells.get(a.cabin)
        if old is None or _cell_key(opt) < _cell_key(old.option):
            row.cells[a.cabin] = CompareCell(opt)
    return list(rows.values())
