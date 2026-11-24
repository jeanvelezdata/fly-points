"""Per-offer "How to book this" plans from the sourced per-program data in data/booking.yaml.
Pure functions; no I/O beyond loading the YAML."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .config import BankRules, Program, load_bank_rules, load_programs, resolve_code
from .models import AwardOffer, Bank, RankedOption
from .paths import DATA_DIR

Alliance = Literal["star", "oneworld", "skyteam", "none"]
PartnerBooking = Literal["online", "phone", "mixed", "unknown"]

ALTERNATIVE_REASON = "Likely bookable with the same partner space; verify availability before transferring."


class ProgramBooking(BaseModel):
    alliance: Alliance
    award_search_url: str
    deep_link: str | None = None  # placeholders: {origin} {destination} {date} {cabin}
    deep_link_verified: bool = False
    partner_booking: PartnerBooking = "unknown"
    phone: str | None = None
    holds: str = "unknown"
    transfer_time: dict[Bank, str] = Field(default_factory=dict)
    gotchas: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    verified: date


class BookingData(BaseModel):
    programs: dict[str, ProgramBooking]
    carriers: dict[str, Alliance]


class BookingStep(BaseModel):
    kind: Literal["verify", "transfer", "book", "warning"]
    text: str
    url: str | None = None


class Alternative(BaseModel):
    program_code: str
    program_name: str
    banks: list[Bank]
    reason: str


class BookingPlan(BaseModel):
    program_code: str
    steps: list[BookingStep]
    alternatives: list[Alternative] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def load_booking() -> BookingData:
    with open(DATA_DIR / "booking.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return BookingData(programs=raw["programs"], carriers=raw["carriers"])


def fill_deep_link(template: str, offer: AwardOffer) -> str:
    """Substitute {origin} {destination} {date} (ISO) {cabin} (Y/W/J/F) in a deep-link template."""
    values = {
        "{origin}": offer.origin,
        "{destination}": offer.destination,
        "{date}": offer.date.isoformat(),
        "{cabin}": offer.cabin.value,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def operating_carrier(flight_number: str) -> str:
    """2-character IATA prefix of a flight number, upper-cased."""
    return flight_number.strip().upper()[:2]


def itinerary_alliance(offer: AwardOffer, carriers: dict[str, Alliance]) -> Alliance | None:
    """The single alliance (star/oneworld/skyteam) every segment's operating carrier belongs to,
    else None (no segments, unknown carrier, non-alliance carrier, or mixed alliances)."""
    if not offer.segments:
        return None
    found = {carriers.get(operating_carrier(s.flight_number)) for s in offer.segments}
    if len(found) != 1:
        return None
    alliance = found.pop()
    return None if alliance in (None, "none") else alliance


def alternatives(
    offer: AwardOffer, data: BookingData, programs: dict[str, Program]
) -> list[Alternative]:
    """Other Chase/Capital One programs in the itinerary's alliance, excluding the offer's own program."""
    alliance = itinerary_alliance(offer, data.carriers)
    if alliance is None:
        return []
    own = resolve_code(offer.program_code, programs)
    out = []
    for code, entry in data.programs.items():
        program = programs.get(code)
        if code == own or entry.alliance != alliance or program is None or not program.banks:
            continue
        banks = [b for b in (Bank.chase, Bank.capital_one) if b in program.banks]
        out.append(
            Alternative(
                program_code=code, program_name=program.name, banks=banks, reason=ALTERNATIVE_REASON
            )
        )
    return out


def _is_known(text: str | None) -> bool:
    return bool(text) and text.strip().lower() != "unknown"


def build_booking_plan(
    option: RankedOption,
    data: BookingData | None = None,
    programs: dict[str, Program] | None = None,
    bank_rules: dict[Bank, BankRules] | None = None,
) -> BookingPlan:
    """Ordered steps to book ``option``: verify, transfer (best bank first), warn, book (+ gotchas).
    Points amounts come from ``option.bank_options``; nothing is re-priced here."""
    data = data or load_booking()
    programs = programs or load_programs()
    bank_rules = bank_rules or load_bank_rules()
    offer = option.offer
    code = resolve_code(offer.program_code, programs)
    program = programs.get(code)
    name = program.name if program else offer.program_name
    info = data.programs.get(code)
    steps: list[BookingStep] = []
    unknowns: list[str] = []

    if offer.estimated:
        steps.append(
            BookingStep(
                kind="warning",
                text="Availability is unknown: this price is an award-chart estimate, not a live search result.",
            )
        )

    flights = ", ".join(s.flight_number for s in offer.segments)
    target = f"flights {flights}" if flights else f"{offer.origin} to {offer.destination}"
    verify_text = (
        f"Search {name} for {target} on {offer.date.isoformat()} in {offer.cabin.to_provider()} "
        "and confirm the award seat shows at this price."
    )
    if offer.booking_url:
        url = offer.booking_url
    elif info and info.deep_link:
        url = fill_deep_link(info.deep_link, offer)
    else:
        url = info.award_search_url if info else None
    steps.append(BookingStep(kind="verify", text=verify_text, url=url))
    if info is None:
        unknowns.append(f"No booking data for program {code}.")

    options = sorted(option.bank_options, key=lambda o: o.bank != option.best_bank)
    if not options:
        unknowns.append("No Chase or Capital One transfer path to this program.")
    for opt in options:
        rules = bank_rules.get(opt.bank)
        ratio = program.banks[opt.bank].ratio_text if program and opt.bank in program.banks else f"{opt.ratio:g}"
        detail = [f"ratio {ratio}"]
        if opt.bonus:
            detail.append(f"current {opt.bonus:.0%} bonus")
        if rules:
            detail.append(f"minimum {rules.min_transfer:,}, increments of {rules.increment:,}")
        speed = info.transfer_time.get(opt.bank) if info else None
        if _is_known(speed):
            detail.append(f"transfer time: {speed}")
        else:
            detail.append("transfer time unknown")
            unknowns.append(f"Transfer time unknown for {opt.bank.label} to {name}.")
        best = " (best value)" if opt.bank == option.best_bank and len(options) > 1 else ""
        steps.append(
            BookingStep(
                kind="transfer",
                text=f"Transfer {opt.bank_points:,} points from {opt.bank.label} to {name}{best} ({'; '.join(detail)}).",
            )
        )

    holds = info.holds if info else "unknown"
    hold_hint = f" Hold policy: {holds.rstrip('.')}." if _is_known(holds) else (
        " Hold policy is unknown; ask whether a hold is possible."
    )
    if not _is_known(holds):
        unknowns.append(f"Hold policy unknown for {name}.")
    steps.append(
        BookingStep(
            kind="warning",
            text="Transfers are irreversible. Confirm the seat is bookable before transferring." + hold_hint,
        )
    )

    phone = info.phone if info else None
    if not phone:
        unknowns.append(f"Phone number unknown for {name}.")
    mode = info.partner_booking if info else "unknown"
    partners = sorted(
        {c for c in map(operating_carrier, (s.flight_number for s in offer.segments)) if resolve_code(c, programs) != code}
    )
    phone_text = f"call {phone}" if phone else "call the program (number not on file)"
    if not partners:
        book_text = f"After the points arrive, book online at {name}." + (f" By phone: {phone}." if phone else "")
    else:
        carriers = ", ".join(partners)
        if mode == "online":
            book_text = f"After the points arrive, book online; partner flights ({carriers}) can be booked on the site."
        elif mode == "phone":
            book_text = f"Partner flights ({carriers}) must be booked by phone: {phone_text}."
        elif mode == "mixed":
            book_text = (
                f"After the points arrive, try booking online; if partner flights ({carriers}) do not show, {phone_text}."
            )
        else:
            book_text = (
                f"It is unknown whether partner flights ({carriers}) can be booked online; "
                f"if they do not show, {phone_text}."
            )
            unknowns.append(f"Unknown whether {name} partner awards can be booked online.")
    steps.append(BookingStep(kind="book", text=book_text, url=info.award_search_url if info else None))
    for gotcha in info.gotchas if info else []:
        steps.append(BookingStep(kind="warning", text=gotcha))

    return BookingPlan(
        program_code=code,
        steps=steps,
        alternatives=alternatives(offer, data, programs),
        unknowns=unknowns,
    )
