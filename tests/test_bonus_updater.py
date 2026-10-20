from datetime import date
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from flypoints import bonus_updater as bu
from flypoints import cli, config
from flypoints.config import Bonus
from flypoints.models import Bank

PAGES = Path(__file__).parent / "fixtures" / "bonus_pages"
TODAY = date(2026, 10, 19)
runner = CliRunner()


def page(name):
    return (PAGES / name).read_text(encoding="utf-8")


def client(atf=None, pm=None):
    """httpx client serving the saved pages (or errors when a page is None)."""

    def handler(req: httpx.Request) -> httpx.Response:
        body = atf if "awardtravelfinder" in req.url.host else pm
        return httpx.Response(200, text=body) if body is not None else httpx.Response(503)

    return httpx.Client(transport=httpx.MockTransport(handler))


def bonus(bank=Bank.chase, program="AC", pct=0.3, start=date(2026, 10, 1), end=date(2026, 10, 31)):
    return Bonus(bank=bank, program=program, bonus=pct, start=start, end=end, source="x", verified=TODAY)


def atf_html(*cards):
    """Minimal ATF-style page. cards: (title, pct, validity text)."""
    out = ""
    for title, pct, valid in cards:
        out += (
            f'<a href="/transfer-bonuses/x"><h4>{title}</h4><span>{pct}</span><div>{valid}</div></a>'
        )
    return f"<html><body>{out}</body></html>"


def pm_html(*rows):
    """Minimal point.me-style table. rows: (bank, destination, pct, expires)."""
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>r</td><td>{d}</td></tr>" for a, b, c, d in rows)
    return (
        "<table><tr><th>Source Program</th><th>Destination Partner</th><th>Bonus</th>"
        f"<th>Effective Ratio</th><th>Expires</th></tr>{body}</table>"
    )


# --- parsing the saved live pages -------------------------------------------------------------


def test_parse_awardtravelfinder_fixture():
    rows = bu.parse_awardtravelfinder(page("awardtravelfinder.html"))
    c1 = [r for r in rows if r.bank == "Capital One Miles"]
    assert [(r.destination, r.percent, r.start, r.end) for r in c1] == [
        ("British Airways Avios", "+20%", date(2026, 10, 1), date(2026, 10, 31))
    ]
    assert len(rows) == 9  # active cards only; expired cards are skipped
    assert not [r for r in rows if r.bank == "Chase Ultimate Rewards"]


def test_parse_pointme_fixture():
    rows = bu.parse_pointme(page("pointme.html"))
    assert len(rows) == 7
    c1 = [r for r in rows if r.bank == "Capital One Miles"]
    assert [(r.destination, r.percent, r.start, r.end) for r in c1] == [
        ("The British Airways Club", "20%", None, date(2026, 10, 31))
    ]


def test_fixtures_end_to_end_cross_source_agreement():
    proposed, warnings = bu.fetch_proposals(
        client=client(page("awardtravelfinder.html"), page("pointme.html")), today=TODAY
    )
    assert len(proposed) == 1  # same bonus on both pages -> deduped, no conflict
    b = proposed[0]
    assert (b.bank, b.program, b.bonus, b.start, b.end) == (
        Bank.capital_one, "BA", 0.2, date(2026, 10, 1), date(2026, 10, 31)
    )
    assert b.source == bu.SOURCES["awardtravelfinder"]  # the one with a real start date
    assert b.verified == TODAY
    assert not [w for w in warnings if "conflict" in w]
    # Chase -> Marriott (expired Oct 15) must not even warn on the 19th
    assert not [w for w in warnings if "Marriott" in w]


def test_parse_returns_none_when_layout_unrecognised():
    assert bu.parse_awardtravelfinder("<html><body>hi</body></html>") is None
    assert bu.parse_pointme("<html><body><table><tr><th>x</th></tr></table></body></html>") is None


# --- mapping and percentages --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,code",
    [
        ("Air Canada Aeroplan", "AC"),
        ("avianca lifemiles", "AV"),
        ("Avianca LifeMiles", "AV"),
        ("Air France KLM Flying Blue", "KL"),
        ("Flying Blue", "KL"),
        ("The British Airways Club", "BA"),
        ("British Airways Avios", "BA"),
        ("Virgin Atlantic Flying Club", "VS"),
        ("JAL (Japan Airlines) Mileage Bank", "JL"),
        ("Qatar Privilege Club Avios", "QR"),
        ("Marriott Bonvoy", None),
        ("Hilton", None),
    ],
)
def test_program_code(name, code):
    assert bu.program_code(name) == code


def test_every_mapped_code_exists_in_programs_yaml():
    assert set(bu.PROGRAM_CODES.values()) <= set(config.load_programs())


@pytest.mark.parametrize(
    "text,pct", [("+30%", 0.3), ("30%", 0.3), ("12.5 %", 0.125), ("Up to 100%", None), ("n/a", None), ("", None)]
)
def test_parse_percent(text, pct):
    assert bu.parse_percent(text) == pct


def test_synthetic_pages_map_and_skip():
    atf = atf_html(
        ("Chase Ultimate Rewards &rarr; Air Canada Aeroplan", "+30%", "Valid: Oct 10 - Nov 05, 2026"),
        ("Chase Ultimate Rewards &rarr; Marriott Bonvoy", "+40%", "Valid: Oct 10 - Nov 05, 2026"),
        ("Chase Ultimate Rewards &rarr; Hyatt-ish Mystery", "+40%", "Valid: Oct 10 - Nov 05, 2026"),
        ("Chase Ultimate Rewards &rarr; JAL Mileage Bank", "+40%", "Valid: Oct 10 - Nov 05, 2026"),  # no Chase->JL
        ("Amex Membership Rewards &rarr; Flying Blue", "+25%", "Valid: Oct 10 - Nov 05, 2026"),
        ("Capital One Miles &rarr; Avianca LifeMiles", "+25%", "Valid: Dec 20 - Jan 10, 2027"),
        ("Capital One Miles &rarr; Etihad Guest", "+25%", "Expired: Sep 30, 2026"),
    )
    proposed, warnings = bu.fetch_proposals(client=client(atf, None), today=TODAY, sources=["awardtravelfinder"])
    got = {(b.bank, b.program): (b.bonus, b.start, b.end) for b in proposed}
    assert got == {
        (Bank.chase, "AC"): (0.3, date(2026, 10, 10), date(2026, 11, 5)),
        (Bank.capital_one, "AV"): (0.25, date(2026, 12, 20), date(2027, 1, 10)),
    }
    text = "\n".join(warnings)
    assert "Marriott" in text and "Mystery" in text and "does not transfer to JL" in text


def test_missing_end_date_is_skipped_with_warning():
    pm = pm_html(("Chase Ultimate Rewards", "Air Canada Aeroplan", "30%", "soon"))
    proposed, warnings = bu.fetch_proposals(client=client(None, pm), today=TODAY, sources=["pointme"])
    assert proposed == []
    assert any("no end date" in w for w in warnings)


def test_pointme_only_uses_today_as_start():
    pm = pm_html(("Chase Ultimate Rewards", "Air Canada Aeroplan", "30%", "November 5, 2026"))
    (b,), _ = bu.fetch_proposals(client=client(None, pm), today=TODAY, sources=["pointme"])
    assert (b.start, b.end, b.bonus) == (TODAY, date(2026, 11, 5), 0.3)


# --- fetch failures, dedupe, conflicts ------------------------------------------------------


def test_one_source_failing_is_a_warning():
    proposed, warnings, parsed = bu.collect(client=client(None, page("pointme.html")), today=TODAY)
    assert parsed == 1
    assert len(proposed) == 1
    assert any(w.startswith("awardtravelfinder: fetch failed") for w in warnings)


def test_all_sources_failing_parses_nothing():
    proposed, warnings, parsed = bu.collect(client=client(None, None), today=TODAY)
    assert (proposed, parsed, len(warnings)) == ([], 0, 2)


def test_unparseable_page_is_a_warning_not_parsed():
    _, warnings, parsed = bu.collect(client=client("<html></html>", None), today=TODAY)
    assert parsed == 0
    assert any("layout may have changed" in w for w in warnings)


def test_conflicting_percentages_dropped_with_warning():
    atf = atf_html(("Chase Ultimate Rewards &rarr; Air Canada Aeroplan", "+30%", "Valid: Oct 10 - Nov 05, 2026"))
    pm = pm_html(("Chase Ultimate Rewards", "Air Canada Aeroplan", "25%", "November 5, 2026"))
    proposed, warnings = bu.fetch_proposals(client=client(atf, pm), today=TODAY)
    assert proposed == []
    assert any("conflict" in w and "30%" in w and "25%" in w for w in warnings)


def test_conflicting_end_dates_dropped_with_warning():
    atf = atf_html(("Chase Ultimate Rewards &rarr; Air Canada Aeroplan", "+30%", "Valid: Oct 10 - Nov 05, 2026"))
    pm = pm_html(("Chase Ultimate Rewards", "Air Canada Aeroplan", "30%", "November 6, 2026"))
    proposed, warnings = bu.fetch_proposals(client=client(atf, pm), today=TODAY)
    assert proposed == []
    assert any("conflict" in w for w in warnings)


# --- diff -------------------------------------------------------------------------------------


def test_diff_added_changed_removed_expired():
    live_same = bonus(program="AC")
    live_changed = bonus(program="AV", pct=0.2)
    live_unlisted = bonus(program="KL")
    expired = bonus(program="BA", start=date(2026, 9, 1), end=date(2026, 9, 30))
    upcoming = bonus(program="IB", start=date(2026, 11, 1), end=date(2026, 11, 30))
    current = [live_same, live_changed, live_unlisted, expired, upcoming]
    proposed = [bonus(program="AC"), bonus(program="AV", pct=0.3, start=TODAY), bonus(program="EI")]

    d = bu.diff(current, proposed, TODAY)

    assert [(b.bank, b.program) for b in d.added] == [(Bank.chase, "EI")]
    assert {b.program for b in d.removed} == {"KL", "BA"}  # unlisted live row + expired row
    (old, new), = d.changed
    assert (old.bonus, new.bonus, new.start) == (0.2, 0.3, live_changed.start)  # original start kept
    assert bool(d)


def test_diff_changed_end_date():
    d = bu.diff([bonus()], [bonus(end=date(2026, 11, 15))], TODAY)
    assert [(o.end, n.end) for o, n in d.changed] == [(date(2026, 10, 31), date(2026, 11, 15))]


def test_diff_no_changes_is_falsy():
    assert not bu.diff([bonus()], [bonus()], TODAY)


def test_diff_no_removal_of_live_rows_when_no_source_parsed():
    expired = bonus(program="BA", start=date(2026, 9, 1), end=date(2026, 9, 30))
    d = bu.diff([bonus(), expired], [], TODAY, sources_parsed=False)
    assert d.removed == [expired]  # expiry needs no source, unlisted does


# --- apply ------------------------------------------------------------------------------------

HEADER = "# Active transfer bonuses.\n# second line\n\n"


def write_file(tmp_path, rows_yaml="[]\n"):
    f = tmp_path / "bonuses.yaml"
    f.write_text(HEADER + rows_yaml, encoding="utf-8")
    return f


def test_apply_round_trip(tmp_path, monkeypatch):
    f = write_file(
        tmp_path,
        "- {bank: chase, program: KL, bonus: 0.2, start: 2026-10-01, end: 2026-10-31}\n"
        "- {bank: chase, program: BA, bonus: 0.2, start: 2026-09-01, end: 2026-09-30}\n"
        "- {bank: capital_one, program: AC, bonus: 0.2, start: 2026-10-01, end: 2026-10-31}\n",
    )
    current = bu.read_all(f)
    assert len(current) == 3  # includes the expired row, unlike config.load_bonuses
    proposed = [
        bonus(Bank.chase, "KL", 0.3, TODAY, date(2026, 11, 1)),  # changed
        bonus(Bank.capital_one, "AC"),  # unchanged except pct -> changed too
        bonus(Bank.capital_one, "AV"),  # added
    ]
    d = bu.diff(current, proposed, TODAY)
    bu.apply(d, f)

    text = f.read_text(encoding="utf-8")
    assert text.startswith(HEADER)
    after = bu.read_all(f)
    assert [(b.bank.value, b.program) for b in after] == [("capital_one", "AC"), ("capital_one", "AV"), ("chase", "KL")]
    kl = after[2]
    assert (kl.bonus, kl.end, kl.start) == (0.3, date(2026, 11, 1), date(2026, 10, 1))

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert {b.program for b in config.load_bonuses(TODAY)} == {"AC", "AV", "KL"}


def test_apply_empty_result_keeps_header_and_loads(tmp_path, monkeypatch):
    f = write_file(tmp_path, "- {bank: chase, program: BA, bonus: 0.2, start: 2026-09-01, end: 2026-09-30}\n")
    bu.apply(bu.diff(bu.read_all(f), [], TODAY), f)
    assert f.read_text(encoding="utf-8") == HEADER + "[]\n"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert config.load_bonuses(TODAY) == []


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = write_file(tmp_path)
    monkeypatch.setattr(bu, "BONUSES_PATH", f)
    proposal = bonus(Bank.capital_one, "BA", 0.2)
    monkeypatch.setattr(bu, "collect", lambda **kw: ([proposal], ["some warning"], 2))
    return f


def test_cli_dry_run_writes_nothing(env):
    before = env.read_text(encoding="utf-8")
    r = runner.invoke(cli.app, ["bonuses-update", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "BA" in r.output and "some warning" in r.output and "Dry run" in r.output
    assert env.read_text(encoding="utf-8") == before


def test_cli_answer_no_writes_nothing(env):
    before = env.read_text(encoding="utf-8")
    r = runner.invoke(cli.app, ["bonuses-update"], input="n\n")
    assert r.exit_code == 0, r.output
    assert "Nothing written" in r.output
    assert env.read_text(encoding="utf-8") == before


def test_cli_answer_yes_writes(env):
    r = runner.invoke(cli.app, ["bonuses-update"], input="y\n")
    assert r.exit_code == 0, r.output
    assert [(b.bank, b.program) for b in bu.read_all(env)] == [(Bank.capital_one, "BA")]


def test_cli_yes_flag_writes_without_prompt(env):
    r = runner.invoke(cli.app, ["bonuses-update", "--yes"])
    assert r.exit_code == 0, r.output
    assert len(bu.read_all(env)) == 1
    assert env.read_text(encoding="utf-8").startswith(HEADER)


def test_cli_no_changes(env, monkeypatch):
    monkeypatch.setattr(bu, "collect", lambda **kw: ([], [], 2))
    r = runner.invoke(cli.app, ["bonuses-update", "--yes"])
    assert "No changes proposed" in r.output
    assert env.read_text(encoding="utf-8") == HEADER + "[]\n"
