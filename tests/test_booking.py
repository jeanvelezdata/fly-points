from datetime import date, datetime

from flypoints.booking import (
    ALTERNATIVE_REASON,
    BookingData,
    ProgramBooking,
    build_booking_plan,
    fill_deep_link,
    load_booking,
)
from flypoints.config import load_bank_rules, load_programs
from flypoints.models import AwardOffer, Bank, BankOption, Cabin, RankedOption, Segment
from flypoints.pricing import build_ranked_option

PROGRAMS = load_programs()
RULES = load_bank_rules()
DATA = load_booking()
NOW = datetime(2026, 10, 20, 12, 0)
ALLIANCES = {"star", "oneworld", "skyteam", "none"}
PARTNER_MODES = {"online", "phone", "mixed", "unknown"}


def seg(number, origin="SJU", destination="MAD"):
    return Segment(
        flight_number=number, origin=origin, destination=destination,
        depart=datetime(2026, 12, 2, 9, 0), arrive=datetime(2026, 12, 2, 20, 0),
    )


def offer(code="AC", flights=("AC1",), **kw):
    fields = dict(
        provider="t", program_code=code, program_name=code, origin="SJU", destination="MAD",
        date=date(2026, 12, 2), cabin=Cabin.J, miles=60000, taxes_usd=50.0, fetched_at=NOW,
        segments=[seg(f) for f in flights],
    )
    fields.update(kw)
    return AwardOffer(**fields)


def ranked(o, bonuses=()):
    return build_ranked_option(o, None, PROGRAMS, RULES, list(bonuses))


def kinds(plan):
    return [s.kind for s in plan.steps]


def test_booking_yaml_covers_every_program():
    assert set(DATA.programs) == set(PROGRAMS)
    for code, entry in DATA.programs.items():
        assert entry.alliance in ALLIANCES, code
        assert entry.partner_booking in PARTNER_MODES, code
        assert entry.award_search_url.startswith("https://"), code
        assert set(entry.transfer_time) <= set(PROGRAMS[code].banks), code
        if entry.deep_link_verified:
            assert entry.deep_link, code
    assert set(DATA.carriers.values()) <= ALLIANCES
    assert all(len(c) == 2 for c in DATA.carriers)


def test_step_order_and_transfer_per_bank_best_first():
    plan = build_booking_plan(ranked(offer("AC")))
    assert kinds(plan)[:5] == ["verify", "transfer", "transfer", "warning", "book"]
    assert plan.steps[1].text.startswith("Transfer 60,000 points from Chase to Air Canada Aeroplan")
    assert "Capital One" in plan.steps[2].text
    assert plan.program_code == "AC"


def test_best_bank_listed_first_when_capital_one_cheaper():
    o = offer("B6", flights=("B61",), miles=10000)
    plan = build_booking_plan(ranked(o))
    transfers = [s for s in plan.steps if s.kind == "transfer"]
    assert "Chase" in transfers[0].text and "(best value)" in transfers[0].text  # 10,000 vs 16,700
    option = ranked(o)
    option.best_bank = Bank.capital_one
    flipped = [s for s in build_booking_plan(option).steps if s.kind == "transfer"]
    assert "Capital One" in flipped[0].text


def test_bonus_in_transfer_step():
    option = RankedOption(
        offer=offer("AC"),
        bank_options=[BankOption(bank=Bank.chase, ratio=1.0, bonus=0.3, bank_points=47000)],
        best_bank=Bank.chase,
        best_bank_points=47000,
    )
    text = [s for s in build_booking_plan(option).steps if s.kind == "transfer"][0].text
    assert "47,000" in text and "30% bonus" in text and "ratio 1:1" in text


def test_fill_deep_link():
    url = fill_deep_link("https://x/?o={origin}&d={destination}&t={date}&c={cabin}", offer())
    assert url == "https://x/?o=SJU&d=MAD&t=2026-12-02&c=J"


def _data_with(code="AC", **kw):
    entry = DATA.programs[code].model_copy(update=kw)
    return BookingData(programs={**DATA.programs, code: entry}, carriers=DATA.carriers)


def test_verify_url_deep_link_then_search_url_then_booking_url():
    template = "https://x/?o={origin}&d={destination}&t={date}"
    with_link = build_booking_plan(ranked(offer("AC")), data=_data_with(deep_link=template))
    assert with_link.steps[0].url == "https://x/?o=SJU&d=MAD&t=2026-12-02"
    assert "AC1" in with_link.steps[0].text and "2026-12-02" in with_link.steps[0].text

    fallback = build_booking_plan(ranked(offer("AC")))
    assert fallback.steps[0].url == DATA.programs["AC"].award_search_url

    given = offer("AC", booking_url="https://book/now")
    plan = build_booking_plan(ranked(given), data=_data_with(deep_link=template))
    assert plan.steps[0].url == "https://book/now"


def test_estimated_offer_starts_with_warning():
    plan = build_booking_plan(ranked(offer("AC", estimated=True)))
    assert plan.steps[0].kind == "warning"
    assert "estimate" in plan.steps[0].text
    assert plan.steps[1].kind == "verify"


def test_alternatives_same_alliance_excluding_own_program():
    plan = build_booking_plan(ranked(offer("AC", flights=("UA1", "LH2"))))
    codes = {a.program_code for a in plan.alternatives}
    assert "AC" not in codes
    assert {"SQ", "UA", "AV", "TK", "TP", "BR"} <= codes
    assert not codes & {"BA", "KL", "AM"}
    sq = next(a for a in plan.alternatives if a.program_code == "SQ")
    assert sq.banks == [Bank.chase, Bank.capital_one]
    assert sq.reason == ALTERNATIVE_REASON


def test_alternatives_none_for_mixed_or_unaligned_itinerary():
    assert build_booking_plan(ranked(offer("AC", flights=("UA1", "BA2")))).alternatives == []
    assert build_booking_plan(ranked(offer("AC", flights=("UA1", "EK2")))).alternatives == []
    assert build_booking_plan(ranked(offer("AC", flights=("ZZ1",)))).alternatives == []
    assert build_booking_plan(ranked(offer("AC", flights=()))).alternatives == []


def test_alias_carrier_is_own_metal_and_excluded_from_alternatives():
    plan = build_booking_plan(ranked(offer("KL", flights=("AF1", "KL2"))))
    assert "KL" not in {a.program_code for a in plan.alternatives}
    assert {"VS", "AM"} <= {a.program_code for a in plan.alternatives}
    book = [s for s in plan.steps if s.kind == "book"][0]
    assert "partner flights" not in book.text


def test_partner_flight_booking_text_by_mode():
    mixed = build_booking_plan(ranked(offer("AC", flights=("LH1",))), data=_data_with(partner_booking="mixed"))
    book = [s for s in mixed.steps if s.kind == "book"][0].text
    assert "LH" in book and "1-800-361-5373" in book
    phone = build_booking_plan(ranked(offer("AC", flights=("LH1",))), data=_data_with(partner_booking="phone"))
    assert "must be booked by phone" in [s for s in phone.steps if s.kind == "book"][0].text


def test_unknowns_recorded_and_gotchas_become_warnings():
    plan = build_booking_plan(ranked(offer("IB", flights=("IB1",))))
    assert any("Phone number unknown" in u for u in plan.unknowns)
    assert any("Hold policy unknown" in u for u in plan.unknowns)
    assert not any("Transfer time unknown" in u for u in plan.unknowns)
    assert [s.text for s in plan.steps if s.kind == "warning"][1:] == DATA.programs["IB"].gotchas

    unknown_time = build_booking_plan(
        ranked(offer("AC")), data=_data_with(transfer_time={Bank.chase: "unknown"})
    )
    assert any("Transfer time unknown for Chase" in u for u in unknown_time.unknowns)
    assert any("Transfer time unknown for Capital One" in u for u in unknown_time.unknowns)


def test_hold_text_in_irreversible_warning():
    plan = build_booking_plan(ranked(offer("KL", flights=("KL1",))))
    warning = next(s for s in plan.steps if s.kind == "warning")
    assert "irreversible" in warning.text and "48 hours" in warning.text


def test_program_without_booking_data_still_plans():
    data = BookingData(programs={}, carriers=DATA.carriers)
    plan = build_booking_plan(ranked(offer("AC")), data=data)
    assert kinds(plan)[0] == "verify" and plan.steps[0].url is None
    assert any("No booking data" in u for u in plan.unknowns)


def test_programbooking_rejects_bad_enum():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProgramBooking(alliance="bogus", award_search_url="https://x", verified=date(2026, 10, 20))
