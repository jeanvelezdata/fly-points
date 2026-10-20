"""Loaders for the YAML files in data/ (programs, bank rules, bonuses, regions)."""
from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction

import yaml
from pydantic import BaseModel

from .models import Bank
from .paths import DATA_DIR


class BankRatio(BaseModel):
    ratio: float  # partner miles per bank point (4:3 -> 0.75)
    ratio_text: str  # as written in the file, e.g. "4:3"
    verified: date


class Program(BaseModel):
    code: str
    name: str
    transfer_time: str
    aliases: list[str] = []
    banks: dict[Bank, BankRatio]


class BankRules(BaseModel):
    min_transfer: int
    increment: int
    portal_cpp: float  # cents per point when booking via the bank's travel portal


class Bonus(BaseModel):
    bank: Bank
    program: str
    bonus: float  # fraction, 0.3 = 30%
    start: date
    end: date
    source: str | None = None
    verified: date | None = None


def _read(name: str):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_ratio(text: str) -> float:
    a, b = text.split(":")
    return float(Fraction(int(b), int(a)))


def load_programs() -> dict[str, Program]:
    """code -> Program. Keyed by canonical code only; use ``resolve_code`` for aliases (AF -> KL)."""
    out = {}
    for code, p in _read("programs.yaml")["programs"].items():
        banks = {
            Bank(bank): BankRatio(
                ratio=_parse_ratio(row["ratio"]), ratio_text=row["ratio"], verified=row["verified"]
            )
            for bank, row in p["banks"].items()
        }
        out[code] = Program(
            code=code,
            name=p["name"],
            transfer_time=p["transfer_time"],
            aliases=p.get("aliases", []),
            banks=banks,
        )
    return out


def resolve_code(code: str, programs: dict[str, Program]) -> str:
    """Return the canonical program code for ``code`` (alias-aware, upper-cased).
    Unknown codes are returned unchanged (upper-cased)."""
    code = code.upper()
    if code in programs:
        return code
    for p in programs.values():
        if code in p.aliases:
            return p.code
    return code


def load_bank_rules() -> dict[Bank, BankRules]:
    return {Bank(b): BankRules(**r) for b, r in _read("programs.yaml")["banks"].items()}


def load_bonuses(today: date | None = None) -> list[Bonus]:
    """Bonuses active on ``today`` (default: today), start/end inclusive."""
    today = today or date.today()
    rows = _read("bonuses.yaml") or []
    return [b for b in (Bonus(**r) for r in rows) if b.start <= today <= b.end]


def load_regions() -> dict[str, list[str]]:
    return _read("regions.yaml")


def expand_airports(token: str) -> list[str]:
    """Region name (case-insensitive) -> its airport list; 3-letter IATA -> [IATA upper-cased].
    Raises ValueError otherwise."""
    token = token.strip()
    for name, airports in load_regions().items():
        if name.lower() == token.lower():
            return list(airports)
    if len(token) == 3 and token.isalpha():
        return [token.upper()]
    raise ValueError(f"Not a region or IATA code: {token!r}")


def stale_rows(days: int = 90, today: date | None = None) -> list[tuple[str, Bank, date]]:
    """(program_code, bank, verified) for rows whose verified date is more than ``days`` old."""
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    return [
        (code, bank, row.verified)
        for code, p in load_programs().items()
        for bank, row in p.banks.items()
        if row.verified < cutoff
    ]
