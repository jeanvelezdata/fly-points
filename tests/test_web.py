import re
import threading
import time
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from flypoints import engine
from flypoints.config import load_bank_rules, load_programs
from flypoints.models import AwardOffer, Cabin, Segment
from flypoints.pricing import build_ranked_option
from flypoints.providers.base import AuthError
from flypoints.web.app import create_app

NOW = datetime(2026, 10, 19, 12, 0)
FORM = {"origins": "SJU", "destinations": "MAD", "date_from": "2026-12-01", "date_to": "2026-12-04",
        "cabin": "J", "max_searches": "3"}


def fake_result():
    def seg(a, b):
        return Segment(flight_number="XX1", origin=a, destination=b, depart=NOW, arrive=NOW)

    def opt(code, miles, taxes, seats, est=False, via=()):
        stops = ["SJU", *via, "MAD"]
        segments = [] if est else [seg(a, b) for a, b in zip(stops, stops[1:])]
        o = AwardOffer(provider="t", program_code=code, program_name=code, origin="SJU", destination="MAD",
                       date=date(2026, 12, 2), cabin=Cabin.J, miles=miles, taxes_usd=taxes, seats=seats,
                       estimated=est, segments=segments, fetched_at=NOW)
        return build_ranked_option(o, None, load_programs(), load_bank_rules(), [])

    opts = [opt("AC", 60000, 50.0, 2, via=["EWR"]), opt("AV", 51100, 200.0, 5),
            opt("KL", 70000, 20.0, None, est=True)]
    return engine.SearchResult(options=opts, searches_planned=2, searches_run=1, searches_cached=1,
                               warnings=["w1"], errors=["e1"])


@pytest.fixture
def client(tmp_path):
    app = create_app(cache=None, search_fn=lambda q, **kw: fake_result(), saved_path=tmp_path / "saved.json")
    return TestClient(app)


def wait_done(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = client.get(f"/jobs/{job_id}/progress").text
        if "every 1s" not in html:
            return html
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def start(client, **overrides):
    r = client.post("/jobs", data={**FORM, **overrides})
    assert r.status_code == 200, r.text
    return re.search(r'data-job-id="(\w+)"', r.text).group(1)


def program_order(html):
    return re.findall(r"<td>(AC|AV|KL)</td>", html)


def test_index_renders_form_and_hints(client):
    html = client.get("/").text
    assert 'name="origins"' in html and "htmx.org@1.9.12" in html
    assert "EUR" in html and "SJU-area" in html  # region hints
    assert 'value="AV"' in html  # program multi-select


@pytest.mark.parametrize("override,message", [
    ({"destinations": "ZZZZ"}, "unknown airport or region"),
    ({"origins": ""}, "enter at least one"),
    ({"date_from": "12/01/2026"}, "From date"),
    ({"date_to": "2026-11-01"}, "before the from date"),
    ({"max_searches": "0"}, "Max searches"),
    ({"programs": "NOPE"}, "Unknown program"),
])
def test_validation_errors(client, override, message):
    r = client.post("/jobs", data={**FORM, **override})
    assert r.status_code == 422 and message in r.text


def test_job_lifecycle_and_results(client):
    job_id = start(client, include_cash="on")
    html = wait_done(client, job_id)
    assert program_order(html) == ["AC", "AV", "KL"]  # engine order preserved
    assert "1 run, 1 cached" in html and "e1" in html and "w1" in html and "EST" in html


def test_sort_and_filter(client):
    job_id = start(client)
    wait_done(client, job_id)
    base = f"/jobs/{job_id}/results"
    assert program_order(client.get(base, params={"sort": "miles", "dir": "asc"}).text) == ["AV", "AC", "KL"]
    assert program_order(client.get(base, params={"sort": "miles", "dir": "desc"}).text) == ["KL", "AC", "AV"]
    assert program_order(client.get(base, params={"sort": "taxes", "dir": "asc"}).text) == ["KL", "AC", "AV"]
    assert program_order(client.get(base, params={"program": "AC"}).text) == ["AC"]
    assert program_order(client.get(base, params={"hide_est": "on"}).text) == ["AC", "AV"]
    assert program_order(client.get(base, params={"max_taxes": "100"}).text) == ["AC", "KL"]
    assert program_order(client.get(base, params={"min_seats": "3"}).text) == ["AV", "KL"]  # unknown seats pass
    assert program_order(client.get(base, params={"max_points": "50000"}).text) == []
    assert "1 of 3 shown" in client.get(base, params={"program": "AC"}).text


def test_stops_column_sort_and_filter(client):
    job_id = start(client)
    base = f"/jobs/{job_id}/results"
    html = wait_done(client, job_id)
    assert "Stops" in html and "nonstop" in html and 'name="max_stops"' in html
    # AC has 1 stop, AV is nonstop, KL is an estimate with unknown routing (sorted last, filtered out)
    assert program_order(client.get(base, params={"sort": "stops", "dir": "asc"}).text) == ["AV", "AC", "KL"]
    assert program_order(client.get(base, params={"sort": "stops", "dir": "desc"}).text) == ["AC", "AV", "KL"]
    assert program_order(client.get(base, params={"max_stops": "0"}).text) == ["AV"]
    assert program_order(client.get(base, params={"max_stops": "1"}).text) == ["AC", "AV"]
    assert program_order(client.get(base, params={"max_stops": ""}).text) == ["AC", "AV", "KL"]


def test_options_passed_to_search_fn(tmp_path):
    seen = {}

    def fn(query, **kw):
        seen.update(query=query, kw=kw)
        return fake_result()

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    wait_done(c, start(c, programs="AV", refresh="on"))
    assert seen["query"].programs == ["AV"] and seen["query"].max_searches == 3
    assert seen["kw"]["refresh"] is True and seen["kw"]["include_cash"] is False


def test_auth_error_shown(tmp_path):
    def fn(query, **kw):
        raise AuthError("Token expired: re-copy it.")

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    assert "Token expired: re-copy it." in wait_done(c, start(c))


def test_saved_searches_roundtrip(client, tmp_path):
    r = client.post("/saved", data={**FORM, "name": "Madrid"})
    assert r.status_code == 200 and "Madrid" in r.text
    assert "Madrid" in client.get("/").text
    assert (tmp_path / "saved.json").exists() and not list(tmp_path.glob("*.tmp"))
    item_id = re.search(r"/saved/(\w+)/run", r.text).group(1)

    run = client.post(f"/saved/{item_id}/run")
    assert run.status_code == 200
    html = wait_done(client, re.search(r'data-job-id="(\w+)"', run.text).group(1))
    assert program_order(html) == ["AC", "AV", "KL"]

    assert "Madrid" not in client.delete(f"/saved/{item_id}").text
    assert "Madrid" not in client.get("/").text
    assert client.post(f"/saved/{item_id}/run").status_code == 404


def test_saved_validation(client):
    r = client.post("/saved", data={**FORM, "name": ""})
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#form-errors"
    assert client.post("/saved", data={**FORM, "destinations": "ZZZZ", "name": "x"}).status_code == 422


def test_unknown_job(client):
    assert client.get("/jobs/nope/progress").status_code == 404
    assert client.get("/jobs/nope/results").status_code == 404


def test_slow_job_does_not_block_and_second_job_queues(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(query, progress=None, **kw):
        calls.append(query)
        started.set()
        progress(1, 4, "working")
        assert release.wait(5)
        return fake_result()

    c = TestClient(create_app(search_fn=slow, saved_path=tmp_path / "s.json"))
    first = start(c)
    assert started.wait(5)
    second = start(c)

    running = c.get(f"/jobs/{first}/progress").text  # responds while the search is blocked
    assert "1/4 searches" in running and "working" in running and "every 1s" in running
    assert "Queued" in c.get(f"/jobs/{second}/progress").text
    assert c.get("/").status_code == 200
    assert len(calls) == 1  # only one live job at a time

    release.set()
    wait_done(c, first)
    wait_done(c, second)
    assert len(calls) == 2
