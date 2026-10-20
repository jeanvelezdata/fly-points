import json
from datetime import date, datetime

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flypoints import cli, engine
from flypoints.config import load_bank_rules, load_programs
from flypoints.models import AwardOffer, Cabin, Segment
from flypoints.pricing import build_ranked_option
from flypoints.providers.base import AuthError

runner = CliRunner()
NOW = datetime(2026, 10, 19, 12, 0)


def result():
    segs = [
        Segment(flight_number="AC1", origin="SJU", destination="YYZ", depart=NOW, arrive=NOW),
        Segment(flight_number="AC2", origin="YYZ", destination="MAD", depart=NOW, arrive=NOW),
    ]
    o = AwardOffer(provider="t", program_code="AC", program_name="Aeroplan", origin="SJU", destination="MAD",
                   date=date(2026, 12, 2), cabin=Cabin.J, miles=60000, taxes_usd=50.0, segments=segs,
                   fetched_at=NOW)
    est = o.model_copy(update={"estimated": True, "segments": [], "program_code": "KL"})
    opts = [build_ranked_option(x, None, load_programs(), load_bank_rules(), []) for x in (o, est)]
    return engine.SearchResult(options=opts, searches_planned=1, searches_run=1, warnings=["w1"], errors=["e1"])


@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def fake(query, **kw):
        calls.update(query=query, kw=kw)
        return result()

    monkeypatch.setattr(engine, "search", fake)
    return calls


def test_table_renders(captured, monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=250))
    r = runner.invoke(cli.app, ["search", "SJU", "MAD,BCN", "--dates", "2026-12-01:2026-12-04", "--cabin", "J"])
    assert r.exit_code == 0, r.output
    assert "SJU-YYZ-MAD" in r.output and "EST" in r.output
    assert "1 run" in r.output and "e1" in r.output and "w1" in r.output
    q = captured["query"]
    assert q.destinations == ["MAD", "BCN"] and q.cabin == Cabin.J and q.max_searches == 40


def test_flags_passed_through(captured):
    runner.invoke(cli.app, ["search", "SJU", "EUR", "--dates", "2026-12-01", "--program", "AC,AV",
                            "--no-cash", "--no-estimates", "--refresh", "--max-searches", "5"])
    assert captured["query"].programs == ["AC", "AV"] and captured["query"].max_searches == 5
    assert captured["kw"]["include_cash"] is False and captured["kw"]["include_estimates"] is False
    assert captured["kw"]["refresh"] is True


def test_single_date(captured):
    runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05"])
    q = captured["query"]
    assert q.date_from == q.date_to == date(2026, 12, 5)


def test_bad_date(captured):
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "12/05/2026"])
    assert r.exit_code != 0 and "query" not in captured


def test_json_output(captured):
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--json"])
    data = json.loads(r.stdout)
    assert len(data["options"]) == 2 and data["searches_run"] == 1


def test_auth_error_exit_2(monkeypatch):
    def boom(query, **kw):
        raise AuthError("token expired")

    monkeypatch.setattr(engine, "search", boom)
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05"])
    assert r.exit_code == 2 and "token expired" in r.output


def test_programs_and_bonuses():
    assert "Aeroplan" in runner.invoke(cli.app, ["programs"]).output
    assert runner.invoke(cli.app, ["bonuses"]).exit_code == 0
