from datetime import date

import pytest

from flypoints import config
from flypoints.config import (
    Bonus,
    expand_airports,
    load_bank_rules,
    load_bonuses,
    load_programs,
    resolve_code,
    stale_rows,
)
from flypoints.models import Bank

CHASE = {"EI", "AC", "KL", "BA", "IB", "B6", "SQ", "WN", "UA", "VS"}
C1_ONE_TO_ONE = {"AM", "AC", "AV", "BA", "CX", "EY", "AY", "KL", "QF", "QR", "SQ", "TP", "TK", "VS"}
C1_ALL = C1_ONE_TO_ONE | {"EK", "BR", "JL", "B6"}


def partners(bank):
    return {c for c, p in load_programs().items() if bank in p.banks}


def test_partner_sets():
    assert partners(Bank.chase) == CHASE
    assert partners(Bank.capital_one) == C1_ALL


def test_ratios():
    p = load_programs()
    assert all(p[c].banks[Bank.chase].ratio == 1.0 for c in CHASE)
    assert all(p[c].banks[Bank.capital_one].ratio == 1.0 for c in C1_ONE_TO_ONE)
    for c in ("EK", "BR", "JL"):
        assert p[c].banks[Bank.capital_one].ratio == 0.75
    assert p["B6"].banks[Bank.capital_one].ratio == 0.6
    assert p["B6"].banks[Bank.capital_one].ratio_text == "5:3"
    assert p["B6"].banks[Bank.chase].ratio == 1.0


def test_every_program_parses():
    for code, p in load_programs().items():
        assert p.code == code and p.name and p.banks
        assert all(r.verified == date(2026, 10, 19) for r in p.banks.values())


def test_alias_af_resolves_to_kl():
    programs = load_programs()
    assert "AF" not in programs
    assert resolve_code("AF", programs) == "KL"
    assert resolve_code("af", programs) == "KL"
    assert resolve_code("ac", programs) == "AC"


def test_bank_rules():
    r = load_bank_rules()
    assert (r[Bank.chase].min_transfer, r[Bank.chase].increment) == (1000, 1000)
    assert (r[Bank.capital_one].min_transfer, r[Bank.capital_one].increment) == (1000, 100)
    assert r[Bank.chase].portal_cpp == 1.0


def test_expand_airports():
    assert expand_airports("EUR")[:3] == ["MAD", "BCN", "LIS"]
    assert expand_airports("eur") == expand_airports("EUR")
    assert expand_airports("SJU-area") == ["SJU", "BQN", "PSE"]
    assert expand_airports("sju") == ["SJU"]
    for bad in ("XX", "ABCD", "12A", ""):
        with pytest.raises(ValueError):
            expand_airports(bad)


def test_real_bonuses_file_is_valid():
    # data/bonuses.yaml holds live data, so check validity rather than contents
    programs = load_programs()
    for b in load_bonuses(date(2026, 10, 19)):
        assert b.bank in programs[b.program].banks


def test_active_bonus_filtering(tmp_path, monkeypatch):
    (tmp_path / "bonuses.yaml").write_text(
        "- {bank: capital_one, program: AC, bonus: 0.3, start: 2026-10-01, end: 2026-10-31}\n"
        "- {bank: chase, program: KL, bonus: 0.25, start: 2026-11-01, end: 2026-11-30}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert [b.program for b in load_bonuses(date(2026, 10, 19))] == ["AC"]
    assert [b.program for b in load_bonuses(date(2026, 10, 31))] == ["AC"]  # end inclusive
    assert [b.program for b in load_bonuses(date(2026, 11, 1))] == ["KL"]
    assert load_bonuses(date(2026, 12, 1)) == []
    assert isinstance(load_bonuses(date(2026, 10, 19))[0], Bonus)


def test_stale_rows():
    assert stale_rows(today=date(2026, 10, 19)) == []
    assert stale_rows(today=date(2027, 1, 17)) == []  # exactly 90 days: not stale
    rows = stale_rows(today=date(2027, 1, 18))
    assert len(rows) == sum(len(p.banks) for p in load_programs().values())
    assert ("AC", Bank.chase, date(2026, 10, 19)) in rows
    assert stale_rows(days=10, today=date(2026, 10, 30)) != []
