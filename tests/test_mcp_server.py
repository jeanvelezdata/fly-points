import asyncio
import json
from datetime import date, datetime

import pytest
from mcp import Client

from flypoints import engine, mcp_server
from flypoints.models import Bank, Cabin, CashFare
from flypoints.providers.base import AuthError
from tests.test_cli import result  # canned SearchResult: one live AC offer + one estimated KL offer

NOW = datetime(2026, 10, 19, 12, 0)


@pytest.fixture
def fake_engine(monkeypatch):
    calls = {}

    def fake(query, **kw):
        calls.update(query=query, kw=kw)
        kw["progress"](1, 1, "msg")
        return result()

    monkeypatch.setattr(engine, "search", fake)
    return calls


def test_search_awards_flattens_options(fake_engine):
    out = asyncio.run(
        mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04", programs="ac", limit=1)
    )
    assert fake_engine["query"].programs == ["AC"]
    assert fake_engine["query"].cabins == [Cabin.J] and fake_engine["query"].max_searches == 4
    assert out["searches_run"] == 1 and out["errors"] == ["e1"] and out["warnings"] == ["w1"]
    assert out["total_options"] == 2 and len(out["options"]) == 1
    row = out["options"][0]
    assert row["route"] == "SJU-YYZ-MAD" and row["program_code"] == "AC" and row["miles"] == 60000
    assert row["best_bank"] and row["best_bank_points"] and not row["estimated"]
    assert row["mixed_cabin_pct"] is None and row["seen"].endswith("ago")
    assert out["options"] and json.dumps(out)


def test_search_awards_cabins_and_filters(fake_engine):
    out = asyncio.run(mcp_server.search_awards(
        "SJU", "MAD", "2026-12-01", "2026-12-04", cabins=["Y", "J"], direct_only=True, max_stops=1,
        carriers=["ac,ua"], days=["mon", "Fri"], max_taxes_usd=100, max_points=80000, min_seats=2,
        banks=["capital_one"], min_cabin_pct=75,
    ))
    q = fake_engine["query"]
    assert "error" not in out
    assert q.cabins == [Cabin.Y, Cabin.J] and q.direct_only and q.max_stops == 1 and q.carriers == ["AC", "UA"]
    assert q.days_of_week == [0, 4] and q.max_taxes_usd == 100 and q.max_points == 80000 and q.min_seats == 2
    assert q.banks == [Bank.capital_one] and q.min_cabin_pct == 75
    for bad in ({"days": ["funday"]}, {"banks": ["amex"]}):
        assert "error" in asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04", **bad))


def test_search_awards_stored_never_runs_live(monkeypatch):
    calls = {}

    def stored(query, **kw):
        calls.update(query=query, kw=kw)
        return result()

    monkeypatch.setattr(engine, "search_stored", stored)
    monkeypatch.setattr(engine, "search", lambda *a, **kw: pytest.fail("live search must not run"))
    out = asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04", stored=True, max_age_hours=6))
    assert calls["kw"]["max_age_hours"] == 6 and out["total_options"] == 2


def test_booking_plan(fake_engine):
    out = asyncio.run(mcp_server.booking_plan("SJU", "MAD", "2026-12-01", "2026-12-04", index=1))
    assert out["option"]["program_code"] == "AC" and out["plan"]["program_code"] == "AC"
    kinds = [s["kind"] for s in out["plan"]["steps"]]
    assert kinds[0] == "verify" and "transfer" in kinds and "book" in kinds and "warning" in kinds
    assert out["plan"]["alternatives"] and isinstance(out["plan"]["unknowns"], list)
    json.dumps(out)
    est = asyncio.run(mcp_server.booking_plan("SJU", "MAD", "2026-12-01", "2026-12-04", index=2))
    assert est["option"]["estimated"] and est["plan"]["steps"][0]["kind"] == "warning"
    for index in (0, 3):
        assert "out of range" in asyncio.run(
            mcp_server.booking_plan("SJU", "MAD", "2026-12-01", "2026-12-04", index=index)
        )["error"]
    assert "error" in asyncio.run(mcp_server.booking_plan("SJU", "MAD", "bad", "2026-12-04"))


def test_sweep_tool(monkeypatch):
    calls = {}

    def fake_sweep(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours, progress=None, stop_event=None):
        calls.update(args=(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours))
        return engine.SweepStats(planned=3, run=2, skipped_fresh=1)

    monkeypatch.setattr(engine, "sweep", fake_sweep)
    out = asyncio.run(mcp_server.sweep("SJU", "EUR", "2027-01-01", "2027-06-30", cabins=["Y", "J"]))
    assert calls["args"] == (["SJU"], ["EUR"], date(2027, 1, 1), date(2027, 6, 30), [Cabin.Y, Cabin.J], 8, 24)
    assert out["planned"] == 3 and out["run"] == 2 and out["stopped"] is False

    def boom(*a, **kw):
        raise AuthError("token expired")

    monkeypatch.setattr(engine, "sweep", boom)
    assert asyncio.run(mcp_server.sweep("SJU", "EUR", "2027-01-01", "2027-06-30")) == {"error": "token expired"}
    assert "error" in asyncio.run(mcp_server.sweep("SJU", "EUR", "bad", "2027-06-30"))
    assert "error" in asyncio.run(mcp_server.sweep("SJU", "EUR", "2027-01-01", "2027-06-30", cabins=["Z"]))


def test_search_awards_errors_are_returned(monkeypatch):
    def boom(query, **kw):
        raise AuthError("token expired, re-copy the cookie")

    monkeypatch.setattr(engine, "search", boom)
    out = asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04"))
    assert out == {"error": "token expired, re-copy the cookie"}
    bad = asyncio.run(mcp_server.search_awards("SJU", "MAD", "12/01/2026", "2026-12-04"))
    assert "error" in bad
    bad = asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04", cabins=["Z"]))
    assert "error" in bad


class FakeCash:
    name = "fake"

    def fares(self, origin, destination, day, cabin):
        return [
            CashFare(provider="fake", origin=origin, destination=destination, date=day, cabin=cabin,
                     price_usd=p, airlines=["IB"], flight_numbers=["IB1"], fetched_at=NOW)
            for p in (5000, 4000, 4500, 6000, 7000, 8000)
        ]


def test_compare_cash(monkeypatch):
    monkeypatch.setattr(mcp_server, "default_cash_provider", FakeCash)
    out = mcp_server.compare_cash("sju", "mad", "2026-12-02")
    assert [f["price_usd"] for f in out["fares"]] == [4000, 4500, 5000, 6000, 7000]
    assert "bank_points" not in out
    out = mcp_server.compare_cash("SJU", "MAD", "2026-12-02", miles=60000, taxes_usd=100, program="AC")
    chase = out["bank_points"]["Chase"]
    assert chase["points"] == 60000 and chase["cpp"] == pytest.approx(6.5)
    assert mcp_server.compare_cash("SJU", "MAD", "bad")["error"]


def test_static_tools():
    progs = mcp_server.list_programs()
    assert any(p["code"] == "AC" for p in progs["programs"]) and "warnings" in progs
    assert "bonuses" in mcp_server.transfer_bonuses()
    assert "EUR" in mcp_server.list_regions()


def test_in_process_client(fake_engine):
    async def run():
        async with Client(mcp_server.mcp) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            progs = await client.call_tool("list_programs", {})
            search = await client.call_tool(
                "search_awards",
                {"origins": "SJU", "destinations": "MAD", "date_from": "2026-12-01", "date_to": "2026-12-04"},
            )
            return names, progs, search

    names, progs, search = asyncio.run(run())
    assert names == {"search_awards", "booking_plan", "sweep", "list_programs", "transfer_bonuses", "compare_cash",
                     "list_regions"}
    assert any(p["code"] == "AC" for p in json.loads(progs.content[0].text)["programs"])
    assert json.loads(search.content[0].text)["options"][0]["program_code"] == "AC"
