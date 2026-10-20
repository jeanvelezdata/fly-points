from datetime import date, datetime

import pytest

from flypoints.config import load_bank_rules, load_bonuses, load_programs, Bonus
from flypoints.models import AwardOffer, Bank, Cabin, CashFare, RankedOption
from flypoints.pricing import (
    bank_options,
    bank_points_needed,
    build_ranked_option,
    cpp,
    portal_points,
    rank,
)

PROGRAMS = load_programs()
RULES = load_bank_rules()
NOW = datetime(2026, 10, 19, 12, 0)


def offer(code="AC", miles=60000, taxes=50.0):
    return AwardOffer(
        provider="t", program_code=code, program_name=code, origin="SJU", destination="MAD",
        date=date(2026, 12, 2), cabin=Cabin.J, miles=miles, taxes_usd=taxes, fetched_at=NOW,
    )


def test_one_to_one():
    assert bank_points_needed(60000, 1.0, 0.0, 1000, 1000) == 60000


def test_four_to_three_jal():
    assert bank_points_needed(60000, 0.75, 0.0, 1000, 100) == 80000


def test_five_to_three_jetblue_rounds_to_100():
    assert bank_points_needed(10000, 0.6, 0.0, 1000, 100) == 16700


def test_bonus_30_percent():
    assert bank_points_needed(60000, 1.0, 0.3, 1000, 1000) == 47000
    assert bank_points_needed(60000, 1.0, 0.3, 1000, 100) == 46200


def test_min_transfer_floor():
    assert bank_points_needed(500, 1.0, 0.0, 1000, 1000) == 1000


def test_capital_one_rounding_matches_pointsyeah():
    assert bank_points_needed(51310, 1.0, 0.0, 1000, 100) == 51400


def test_exact_multiple_not_bumped():
    # 0.75 ratio: 60,000 miles is exactly 80,000 points; float error must not push it to 80,100
    assert bank_points_needed(45000, 0.75, 0.0, 1000, 100) == 60000


def test_bank_options_overlap_program():
    opts = bank_options("AC", 51310, PROGRAMS, RULES, [])
    assert [(o.bank, o.bank_points) for o in opts] == [(Bank.chase, 52000), (Bank.capital_one, 51400)]


def test_bank_options_single_bank_and_alias_and_unknown():
    assert [o.bank for o in bank_options("JL", 60000, PROGRAMS, RULES, [])] == [Bank.capital_one]
    assert [o.bank for o in bank_options("AF", 60000, PROGRAMS, RULES, [])] == [Bank.chase, Bank.capital_one]
    assert bank_options("AA", 60000, PROGRAMS, RULES, []) == []


def test_bank_options_applies_bonus_for_one_bank_only():
    bonus = Bonus(bank=Bank.capital_one, program="AC", bonus=0.3, start=date(2026, 10, 1), end=date(2026, 10, 31))
    opts = {o.bank: o for o in bank_options(offer(), 60000, PROGRAMS, RULES, [bonus])}
    assert opts[Bank.chase].bank_points == 60000
    assert opts[Bank.capital_one].bank_points == 46200
    assert opts[Bank.capital_one].bonus == 0.3


def test_cpp():
    # (4766 - 100) / 80000 * 100 = 5.8325
    assert cpp(4766, 100, 80000) == pytest.approx(5.8325)
    assert cpp(None, 100, 80000) is None
    assert cpp(500, 100, 0) is None
    assert cpp(50, 100, 10000) == pytest.approx(-0.5)


def test_portal_points():
    assert portal_points(302.0, RULES) == {Bank.chase: 30200, Bank.capital_one: 30200}
    assert portal_points(302.5, RULES)[Bank.chase] == 30250


def _ranked(points, taxes, cpp_):
    return RankedOption(offer=offer(taxes=taxes), best_bank_points=points, cpp=cpp_)


def test_rank_ordering():
    a = _ranked(60000, 100, 2.0)
    b = _ranked(50000, 200, 1.0)
    c = _ranked(60000, 100, 3.0)
    d = _ranked(60000, 50, None)
    e = _ranked(None, 0, None)
    f = _ranked(60000, 100, None)
    assert rank([a, b, c, d, e, f]) == [b, d, c, a, f, e]


def test_build_ranked_option_overlap_note_and_cpp():
    cash = CashFare(provider="t", origin="SJU", destination="MAD", date=date(2026, 12, 2), cabin=Cabin.J,
                    price_usd=4766, fetched_at=NOW)
    r = build_ranked_option(offer(miles=51310, taxes=100.0), cash, PROGRAMS, RULES, [])
    assert r.best_bank == Bank.capital_one and r.best_bank_points == 51400
    assert r.note == "cheaper via Capital One (−600 pts)"
    assert r.cpp == pytest.approx((4766 - 100) / 51400 * 100)
    assert r.portal_points[Bank.chase] == 476600


def test_build_ranked_option_tie_prefers_chase_and_no_cash():
    r = build_ranked_option(offer(miles=60000), None, PROGRAMS, RULES, [])
    assert r.best_bank == Bank.chase and r.note is None
    assert r.cpp is None and r.portal_points is None


def test_build_ranked_option_unreachable_program():
    assert build_ranked_option(offer(code="AA"), None, PROGRAMS, RULES, []) is None


def test_offer_key_and_cabin_mapping():
    assert offer().key == ("AC", date(2026, 12, 2), Cabin.J, ())
    assert Cabin.from_provider("premium economy") is Cabin.W
    assert Cabin.from_provider("BUSINESS") is Cabin.J
    assert Cabin.J.to_provider() == "Business"
    with pytest.raises(ValueError):
        Cabin.from_provider("coach")
