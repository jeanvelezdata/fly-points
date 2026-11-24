import json
import threading
from datetime import date, datetime, timedelta

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flypoints import cli, engine
from flypoints.config import load_bank_rules, load_programs
from flypoints.models import AwardOffer, Bank, Cabin, Segment
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
    assert q.destinations == ["MAD", "BCN"] and q.cabins == [Cabin.J] and q.max_searches == 40


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


def wide(monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=250))


def test_multiple_cabins_and_filter_flags(captured):
    r = runner.invoke(cli.app, [
        "search", "SJU", "MAD", "--dates", "2026-12-01", "--cabin", "Y,J", "--cabin", "F", "--direct",
        "--max-stops", "1", "--carriers", "ac,ua", "--days", "mon,fri", "--max-taxes", "100", "--max-points", "70000",
        "--min-seats", "2", "--bank", "chase", "--bank", "capital_one", "--min-cabin-pct", "80",
    ])
    assert r.exit_code == 0, r.output
    q = captured["query"]
    assert q.cabins == [Cabin.Y, Cabin.J, Cabin.F]
    assert (q.direct_only, q.max_stops, q.carriers, q.days_of_week) == (True, 1, ["AC", "UA"], [0, 4])
    assert (q.max_taxes_usd, q.max_points, q.min_seats, q.min_cabin_pct) == (100, 70000, 2, 80)
    assert q.banks == [Bank.chase, Bank.capital_one]


@pytest.mark.parametrize("flags", [["--cabin", "Z"], ["--days", "funday"], ["--bank", "amex"], ["--view", "grid"]])
def test_bad_flag_values(captured, flags):
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", *flags])
    assert r.exit_code != 0 and "query" not in captured


def test_seen_and_mixed_cabin_columns(monkeypatch):
    wide(monkeypatch)
    res = result()
    live, est = res.options
    live.offer.fetched_at = datetime.now() - timedelta(hours=3, minutes=5)
    live.offer.mixed_cabin_pct = 28
    monkeypatch.setattr(engine, "search", lambda q, **kw: res)
    out = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05"]).output
    assert "Seen" in out and "3h ago" in out
    assert "J*" in out and "72% J" in out
    live.offer.mixed_cabin_pct = None
    out = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05"]).output
    assert "J?" in out  # unknown for the live offer; the estimate shows a plain J


def test_stored_uses_search_stored_only(monkeypatch):
    wide(monkeypatch)
    calls = {}

    def stored(query, **kw):
        calls.update(query=query, kw=kw)
        return result()

    monkeypatch.setattr(engine, "search_stored", stored)
    monkeypatch.setattr(engine, "search", lambda *a, **kw: pytest.fail("live search must not run"))
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--stored", "--max-age-hours", "6"])
    assert r.exit_code == 0, r.output
    assert calls["kw"]["max_age_hours"] == 6 and calls["kw"]["include_estimates"] is True
    assert "SJU-YYZ-MAD" in r.output and "stored results only" in r.output


def test_compare_view(captured, monkeypatch):
    wide(monkeypatch)
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--view", "compare"])
    assert r.exit_code == 0, r.output
    for text in ("Y", "W", "J", "F", "SJU-YYZ-MAD", "60,000 mi", "2026-12-02", "~"):
        assert text in r.output
    assert "Best bank pts" not in r.output


def test_book_prints_booking_panel(captured, monkeypatch):
    wide(monkeypatch)
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--book", "1"])
    assert r.exit_code == 0, r.output
    assert "How to book #1: AC SJU-YYZ-MAD" in r.output
    for text in ("1. VERIFY:", "TRANSFER:", "WARNING:", "BOOK:", "Alternatives", "https://"):
        assert text in r.output
    est = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--book", "2"]).output
    assert "award-chart estimate" in est and "Unknown (not guessed)" in est  # works for estimated offers too


def test_depart_arrive_columns_and_booking_flights(monkeypatch):
    wide(monkeypatch)
    res = result()
    first, second = res.options[0].offer.segments
    first.depart, first.arrive = datetime(2026, 12, 2, 7, 15), datetime(2026, 12, 2, 11, 0)
    second.depart, second.arrive = datetime(2026, 12, 2, 18, 40), datetime(2026, 12, 3, 8, 5)
    monkeypatch.setattr(engine, "search", lambda q, **kw: res)
    out = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--book", "1"]).output
    assert "Depart" in out and "Arrive" in out and "07:15" in out and "08:05+1" in out
    assert "AC1 SJU 2026-12-02 07:15 -> YYZ 11:00" in out and "AC2 YYZ 2026-12-02 18:40 -> MAD 08:05+1" in out


def test_book_out_of_range(captured):
    r = runner.invoke(cli.app, ["search", "SJU", "MAD", "--dates", "2026-12-05", "--book", "9"])
    assert r.exit_code == 1 and "there are 2 results" in r.output


def test_sweep_command(monkeypatch):
    calls = {}

    def fake_sweep(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours, progress=None, stop_event=None):
        calls.update(args=(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours), stop=stop_event)
        progress(1, 2, "msg")
        return engine.SweepStats(planned=5, skipped_fresh=1, skipped_cap=2, run=2, errors=["boom"])

    monkeypatch.setattr(engine, "sweep", fake_sweep)
    r = runner.invoke(cli.app, ["sweep", "SJU", "EUR", "--dates", "2026-12-01:2027-09-30", "--cabin", "Y,J",
                                "--max-searches", "9", "--fresh-hours", "12"])
    assert r.exit_code == 0, r.output
    assert calls["args"] == (["SJU"], ["EUR"], date(2026, 12, 1), date(2027, 9, 30), [Cabin.Y, Cabin.J], 9, 12)
    assert "5 windows planned" in r.output and "1 already fresh" in r.output and "2 run" in r.output
    assert "boom" in r.output and "stopped early" not in r.output


def test_sweep_ctrl_c_sets_stop_event_and_prints_stats(monkeypatch):
    class CtrlC(threading.Thread):
        """Raises KeyboardInterrupt from the first timed join, as Ctrl-C would in the main thread."""

        fired = False

        def join(self, timeout=None):
            if timeout is not None and not CtrlC.fired:
                CtrlC.fired = True
                raise KeyboardInterrupt
            super().join(timeout)

    seen = {}

    def fake_sweep(*args, progress=None, stop_event=None):
        seen["stopped"] = stop_event.wait(5)  # returns only once the CLI sets the event
        return engine.SweepStats(planned=4, run=1, stopped=True)

    monkeypatch.setattr(cli.threading, "Thread", CtrlC)
    monkeypatch.setattr(engine, "sweep", fake_sweep)
    r = runner.invoke(cli.app, ["sweep", "SJU", "MAD", "--dates", "2026-12-01:2026-12-31"])
    assert seen["stopped"] is True
    assert r.exit_code == 0 and "stopped early" in r.output and "4 windows planned" in r.output


def test_sweep_errors(monkeypatch):
    def auth(*a, **kw):
        raise AuthError("token expired")

    monkeypatch.setattr(engine, "sweep", auth)
    r = runner.invoke(cli.app, ["sweep", "SJU", "MAD", "--dates", "2026-12-01:2026-12-31"])
    assert r.exit_code == 2 and "token expired" in r.output
    monkeypatch.setattr(engine, "sweep", lambda *a, **kw: (_ for _ in ()).throw(ValueError("beyond the horizon")))
    r = runner.invoke(cli.app, ["sweep", "SJU", "MAD", "--dates", "2030-12-01:2030-12-31"])
    assert r.exit_code == 1 and "beyond the horizon" in r.output
    assert runner.invoke(cli.app, ["sweep", "SJU", "MAD", "--dates", "2026-12-01", "--cabin", "Z"]).exit_code != 0
