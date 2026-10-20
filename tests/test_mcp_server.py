import asyncio
import json
from datetime import date, datetime

import pytest
from mcp import Client

from flypoints import engine, mcp_server
from flypoints.models import Cabin, CashFare
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
    assert fake_engine["query"].cabin is Cabin.J and fake_engine["query"].max_searches == 4
    assert out["searches_run"] == 1 and out["errors"] == ["e1"] and out["warnings"] == ["w1"]
    assert out["total_options"] == 2 and len(out["options"]) == 1
    row = out["options"][0]
    assert row["route"] == "SJU-YYZ-MAD" and row["program_code"] == "AC" and row["miles"] == 60000
    assert row["best_bank"] and row["best_bank_points"] and not row["estimated"]
    json.dumps(out)


def test_search_awards_errors_are_returned(monkeypatch):
    def boom(query, **kw):
        raise AuthError("token expired, re-copy the cookie")

    monkeypatch.setattr(engine, "search", boom)
    out = asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04"))
    assert out == {"error": "token expired, re-copy the cookie"}
    bad = asyncio.run(mcp_server.search_awards("SJU", "MAD", "12/01/2026", "2026-12-04"))
    assert "error" in bad
    bad = asyncio.run(mcp_server.search_awards("SJU", "MAD", "2026-12-01", "2026-12-04", cabin="Z"))
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
    assert names == {"search_awards", "list_programs", "transfer_bonuses", "compare_cash", "list_regions"}
    assert any(p["code"] == "AC" for p in json.loads(progs.content[0].text)["programs"])
    assert json.loads(search.content[0].text)["options"][0]["program_code"] == "AC"
