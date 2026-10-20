"""Bank-point math, cents-per-point and ranking. Pure functions; no I/O."""
from __future__ import annotations

import math
from fractions import Fraction

from .config import BankRules, Bonus, Program, resolve_code
from .models import AwardOffer, Bank, BankOption, CashFare, RankedOption


def bank_points_needed(
    miles: int, ratio: float, bonus: float, min_transfer: int, increment: int
) -> int:
    """Smallest bank-point amount P, a multiple of ``increment`` and >= ``min_transfer``, with
    floor(P * ratio * (1 + bonus)) >= miles. ``ratio`` is partner miles per bank point;
    ``bonus`` a fraction (0.3 = 30%). Exact rational math."""
    r = Fraction(str(ratio)) * (1 + Fraction(str(bonus)))
    needed = max(math.ceil(Fraction(miles) / r), min_transfer)
    return math.ceil(needed / increment) * increment


def bank_options(
    offer_or_program_code: AwardOffer | str,
    miles: int,
    programs: dict[str, Program],
    bank_rules: dict[Bank, BankRules],
    bonuses: list[Bonus],
) -> list[BankOption]:
    """One BankOption per bank that can transfer to the program (chase first). ``bonuses`` should
    already be filtered to active ones (``load_bonuses``); the best matching bonus is applied."""
    code = (
        offer_or_program_code
        if isinstance(offer_or_program_code, str)
        else offer_or_program_code.program_code
    )
    code = resolve_code(code, programs)
    program = programs.get(code)
    if program is None:
        return []
    out = []
    for bank in (Bank.chase, Bank.capital_one):
        row = program.banks.get(bank)
        if row is None:
            continue
        bonus = max(
            (b.bonus for b in bonuses if b.bank == bank and resolve_code(b.program, programs) == code),
            default=0.0,
        )
        rules = bank_rules[bank]
        points = bank_points_needed(miles, row.ratio, bonus, rules.min_transfer, rules.increment)
        out.append(BankOption(bank=bank, ratio=row.ratio, bonus=bonus, bank_points=points))
    return out


def cpp(cash_price_usd: float | None, taxes_usd: float, bank_points: int | None) -> float | None:
    """Cents per bank point: (cash - taxes) / bank_points * 100. None if cash or points missing/0.
    May be negative."""
    if cash_price_usd is None or not bank_points:
        return None
    return (cash_price_usd - taxes_usd) / bank_points * 100


def portal_points(cash_price_usd: float, bank_rules: dict[Bank, BankRules]) -> dict[Bank, int]:
    """Points the cash fare would cost via each bank's travel portal: ceil(cash / (portal_cpp/100))."""
    return {
        bank: math.ceil(round(cash_price_usd / (rules.portal_cpp / 100), 6))
        for bank, rules in bank_rules.items()
    }


def rank(options: list[RankedOption]) -> list[RankedOption]:
    """Sort by best_bank_points asc (None last), then taxes asc, then cpp desc (None last)."""

    def key(o: RankedOption):
        return (
            o.best_bank_points is None,
            o.best_bank_points or 0,
            o.offer.taxes_usd,
            o.cpp is None,
            -(o.cpp or 0.0),
        )

    return sorted(options, key=key)


def build_ranked_option(
    offer: AwardOffer,
    cash_fare: CashFare | None,
    programs: dict[str, Program],
    bank_rules: dict[Bank, BankRules],
    bonuses: list[Bonus],
) -> RankedOption | None:
    """Price an offer through each bank. None if neither bank reaches the program.
    best_bank = fewest points (tie -> Chase)."""
    options = bank_options(offer, offer.miles, programs, bank_rules, bonuses)
    if not options:
        return None
    best = min(options, key=lambda o: o.bank_points)  # min() keeps the first (chase) on ties
    note = None
    if len(options) == 2 and options[0].bank_points != options[1].bank_points:
        diff = abs(options[0].bank_points - options[1].bank_points)
        note = f"cheaper via {best.bank.label} (−{diff:,} pts)"
    price = cash_fare.price_usd if cash_fare else None
    return RankedOption(
        offer=offer,
        bank_options=options,
        best_bank=best.bank,
        best_bank_points=best.bank_points,
        cash_fare=cash_fare,
        cpp=cpp(price, offer.taxes_usd, best.bank_points),
        portal_points=portal_points(price, bank_rules) if price is not None else None,
        note=note,
    )
