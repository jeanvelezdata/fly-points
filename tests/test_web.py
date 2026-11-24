import json
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

    def opt(code, miles, taxes, seats, est=False, via=(), mixed=None):
        stops = ["SJU", *via, "MAD"]
        segments = [] if est else [seg(a, b) for a, b in zip(stops, stops[1:])]
        o = AwardOffer(provider="t", program_code=code, program_name=code, origin="SJU", destination="MAD",
                       date=date(2026, 12, 2), cabin=Cabin.J, miles=miles, taxes_usd=taxes, seats=seats,
                       estimated=est, segments=segments, fetched_at=NOW, mixed_cabin_pct=mixed)
        return build_ranked_option(o, None, load_programs(), load_bank_rules(), [])

    opts = [opt("AC", 60000, 50.0, 2, via=["EWR"], mixed=28), opt("AV", 51100, 200.0, 5),
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


def test_depart_arrive_columns_and_sort(tmp_path):
    def fn(query, **kw):
        res = fake_result()
        ac, av, _ = res.options
        first, second = ac.offer.segments
        first.depart, first.arrive = datetime(2026, 12, 2, 20, 0), datetime(2026, 12, 2, 23, 10)
        second.depart, second.arrive = datetime(2026, 12, 3, 1, 0), datetime(2026, 12, 3, 9, 30)
        av.offer.segments[0].depart = datetime(2026, 12, 2, 6, 5)
        return res

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    jid = start(c)
    html = wait_done(c, jid)
    assert ">Depart" in html and "<th>Arrive</th>" in html
    assert "<td>20:00</td><td>09:30+1</td>" in html and "<td>06:05</td><td>12:00</td>" in html
    assert "<td>-</td><td>-</td>" in html  # the estimate has no times
    base = f"/jobs/{jid}/results"
    assert program_order(c.get(base, params={"sort": "depart", "dir": "asc"}).text) == ["AV", "AC", "KL"]
    assert program_order(c.get(base, params={"sort": "depart", "dir": "desc"}).text) == ["AC", "AV", "KL"]
    book = c.get(f"/jobs/{jid}/book/0").text
    assert "XX1 SJU 2026-12-02 20:00 -&gt; EWR 23:10" in book and "XX1 EWR 2026-12-03 01:00 -&gt; MAD 09:30<" in book


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


def test_index_has_new_controls(client):
    html = client.get("/").text
    for text in ('name="cabins" value="Y"', 'name="cabins" value="J" checked', 'name="direct_only"', 'name="carriers"',
                 'name="days_of_week" value="0"', 'name="banks" value="chase"', 'name="min_cabin_pct"',
                 'hx-post="/jobs/stored"', 'name="max_age_hours"', 'hx-post="/sweeps"', 'name="fresh_hours"'):
        assert text in html


def test_multiple_cabins_and_filters_reach_the_query(tmp_path):
    seen = {}

    def fn(query, **kw):
        seen["query"] = query
        return fake_result()

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    data = {**FORM, "cabins": ["Y", "J"], "direct_only": "on", "max_stops": "1", "carriers": "ac, ua",
            "days_of_week": ["0", "4"], "max_taxes_usd": "100", "max_points": "70000", "min_seats": "2",
            "banks": ["chase", "capital_one"], "min_cabin_pct": "80"}
    data.pop("cabin")
    r = c.post("/jobs", data=data)
    assert r.status_code == 200, r.text
    wait_done(c, job_id(r.text))
    q = seen["query"]
    assert q.cabins == [Cabin.Y, Cabin.J] and q.direct_only and q.max_stops == 1 and q.carriers == ["AC", "UA"]
    assert q.days_of_week == [0, 4] and q.max_taxes_usd == 100 and q.max_points == 70000 and q.min_seats == 2
    assert [b.value for b in q.banks] == ["chase", "capital_one"] and q.min_cabin_pct == 80
    assert "Y,J" in r.text  # job title


@pytest.mark.parametrize("override,message", [
    ({"cabin": ""}, "choose at least one"),
    ({"cabin": "Z"}, "choose at least one"),
    ({"max_points": "lots"}, "Max points"),
    ({"min_seats": "-1"}, "Min seats"),
    ({"days_of_week": ["9"]}, "Days"),
    ({"banks": ["amex"]}, "Unknown bank"),
])
def test_new_field_validation(client, override, message):
    r = client.post("/jobs", data={**FORM, **override})
    assert r.status_code == 422 and message in r.text


def test_seen_column_and_mixed_cabin_badges(client):
    html = wait_done(client, start(client))
    assert "<th>Seen</th>" in html and "ago</td>" in html
    assert html.count('class="mix"') == 2  # AC known mix, AV unknown; the estimate has none
    assert "Mixed cabin: 72% J in J" in html and "cabin mix unknown" in html


def test_new_result_filters(client):
    jid = start(client)
    wait_done(client, jid)
    base = f"/jobs/{jid}/results"
    assert program_order(client.get(base, params={"carriers": "XX"}).text) == ["AC", "AV"]  # estimate has no routing
    assert program_order(client.get(base, params={"carriers": "AA, ua"}).text) == []
    assert program_order(client.get(base, params={"days": ["2"]}).text) == ["AC", "AV", "KL"]  # 2026-12-02 is a Wednesday
    assert program_order(client.get(base, params={"days": ["0", "4"]}).text) == []
    assert program_order(client.get(base, params={"min_cabin_pct": "80"}).text) == ["AV", "KL"]  # AC is 72% in cabin
    assert program_order(client.get(base, params={"min_cabin_pct": "70"}).text) == ["AC", "AV", "KL"]


def test_compare_view(client):
    jid = start(client)
    wait_done(client, jid)
    base = f"/jobs/{jid}/results"
    html = client.get(base, params={"view": "compare"}).text
    assert 'class="compare"' in html and program_order(html) == ["AC", "AV", "KL"]
    for c in "YWJF":
        assert f'<th class="num">{c}</th>' in html
    assert "<b>~" in html  # estimate marker
    assert f"/jobs/{jid}/book/1" in html
    assert 'aria-pressed="true"' in html
    assert program_order(client.get(base, params={"view": "compare", "program": "AC"}).text) == ["AC"]
    assert program_order(client.get(base, params={"view": "compare", "hide_est": "on"}).text) == ["AC", "AV"]
    assert 'class="compare"' not in client.get(base, params={"view": "list"}).text


def test_booking_partial(tmp_path):
    def fn(query, **kw):
        res = fake_result()
        for seg in res.options[0].offer.segments:
            seg.flight_number = "AC1"  # a known alliance, so alternatives are offered
        return res

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    jid = start(c)
    wait_done(c, jid)
    assert f'hx-get="/jobs/{jid}/book/0"' in c.get(f"/jobs/{jid}/results").text
    r = c.get(f"/jobs/{jid}/book/0")
    assert r.status_code == 200
    html = r.text
    for text in ('class="step verify"', 'class="step transfer"', 'class="step warning"', 'class="step book"',
                 "Alternatives", 'target="_blank" rel="noopener noreferrer"'):
        assert text in html, text
    assert html.index("step verify") < html.index("step transfer") < html.index("step book")
    est = c.get(f"/jobs/{jid}/book/2").text  # the estimated KL offer
    assert "award-chart estimate" in est and 'class="step warning"' in est
    assert "Unknown, not guessed" in est and "Phone number unknown" in est


def test_booking_partial_errors_and_unsafe_urls(tmp_path):
    def fn(query, **kw):
        res = fake_result()
        res.options[0].offer.booking_url = "javascript:alert(1)"
        return res

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json"))
    jid = start(c)
    wait_done(c, jid)
    assert "javascript:" not in c.get(f"/jobs/{jid}/book/0").text
    assert "javascript:" not in c.get(f"/jobs/{jid}/results").text
    for path in (f"/jobs/{jid}/book/3", f"/jobs/{jid}/book/-1", "/jobs/nope/book/0"):
        assert c.get(path).status_code == 404


def test_stored_search_is_instant_and_never_calls_live(tmp_path):
    seen = {}

    def stored(query, **kw):
        seen.update(query=query, kw=kw)
        return fake_result()

    def live(query, **kw):
        raise AssertionError("live search must not run")

    c = TestClient(create_app(search_fn=live, stored_fn=stored, saved_path=tmp_path / "s.json"))
    r = c.post("/jobs/stored", data={**FORM, "max_age_hours": "6", "include_estimates": "on"})
    assert r.status_code == 200 and "every 1s" not in r.text  # finished, nothing to poll
    assert program_order(r.text) == ["AC", "AV", "KL"] and "stored results only" in r.text and "(stored)" in r.text
    assert seen["kw"]["max_age_hours"] == 6 and seen["kw"]["include_estimates"] is True
    assert seen["query"].cabins == [Cabin.J]
    assert c.post("/jobs/stored", data={**FORM, "origins": ""}).status_code == 422
    assert c.post("/jobs/stored", data={**FORM, "max_age_hours": "x"}).status_code == 422
    results = f"/jobs/{job_id(r.text)}/results"
    assert "1 of 3 shown" in c.get(results, params={"program": "AC"}).text


def test_stored_error_is_shown(tmp_path):
    def stored(query, **kw):
        raise ValueError("Start date 2020-01-01 is in the past")

    c = TestClient(create_app(stored_fn=stored, saved_path=tmp_path / "s.json"))
    assert "Start date 2020-01-01 is in the past" in c.post("/jobs/stored", data=FORM).text


SWEEP = {"origins": "SJU", "destinations": "EUR", "date_from": "2027-01-01", "date_to": "2027-09-30",
         "cabins": ["Y", "J"], "max_searches": "12", "fresh_hours": "6"}


def job_id(html):
    return re.search(r'data-job-id="(\w+)"', html).group(1)


def settle_sweep(c, jid, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = c.get(f"/sweeps/{jid}/progress").text
        if "every 1s" not in html:
            return html
        time.sleep(0.02)
    raise AssertionError("sweep did not finish")


def test_sweep_job_stop_and_final_stats(tmp_path):
    started, calls = threading.Event(), {}

    def sweep(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours, progress=None, stop_event=None):
        calls.update(args=(origins, dests, d_from, d_to, cabins, max_searches, fresh_hours))
        started.set()
        progress(1, 12, "searching")
        assert stop_event.wait(5)  # runs until the Stop button is pressed
        return engine.SweepStats(planned=20, skipped_fresh=3, skipped_cap=5, run=1, errors=["boom"], stopped=True)

    c = TestClient(create_app(sweep_fn=sweep, saved_path=tmp_path / "s.json"))
    r = c.post("/sweeps", data=SWEEP)
    assert r.status_code == 200 and "Sweep SJU to EUR" in r.text
    jid = job_id(r.text)
    assert started.wait(5)
    running = c.get(f"/sweeps/{jid}/progress").text
    assert "1/12 searches" in running and "every 1s" in running and f"/sweeps/{jid}/stop" in running
    assert calls["args"] == (["SJU"], ["EUR"], date(2027, 1, 1), date(2027, 9, 30), [Cabin.Y, Cabin.J], 12, 6)

    stopping = c.post(f"/sweeps/{jid}/stop").text
    assert "Stopping..." in stopping or "Stopped early" in stopping
    html = settle_sweep(c, jid)
    assert "Stopped early." in html and "20 windows planned" in html and "3 already fresh" in html
    assert "5 over the cap (12)" in html and "boom" in html


def test_sweep_error_validation_and_unknown(tmp_path):
    def sweep(*args, **kw):
        raise AuthError("Token expired: re-copy it.")

    c = TestClient(create_app(sweep_fn=sweep, saved_path=tmp_path / "s.json"))
    html = settle_sweep(c, job_id(c.post("/sweeps", data=SWEEP).text))
    assert "Sweep failed." in html and "Token expired" in html
    assert "unknown airport" in c.post("/sweeps", data={**SWEEP, "destinations": "ZZZZ"}).text
    assert c.post("/sweeps", data={**SWEEP, "fresh_hours": "soon"}).status_code == 422
    assert c.post("/sweeps", data={**SWEEP, "cabins": []}).status_code == 422
    assert c.post("/sweeps", data={k: v for k, v in SWEEP.items() if k != "date_to"}).status_code == 200  # to = from
    assert c.get("/sweeps/nope/progress").status_code == 404
    assert c.post("/sweeps/nope/stop").status_code == 404


def test_sweeps_and_searches_share_one_queue(tmp_path):
    release, started = threading.Event(), threading.Event()

    def sweep(*args, progress=None, stop_event=None):
        started.set()
        assert release.wait(5)
        return engine.SweepStats()

    c = TestClient(create_app(sweep_fn=sweep, search_fn=lambda q, **kw: fake_result(), saved_path=tmp_path / "s.json"))
    c.post("/sweeps", data=SWEEP)
    assert started.wait(5)
    queued = start(c)
    assert "Queued" in c.get(f"/jobs/{queued}/progress").text
    release.set()
    wait_done(c, queued)


def test_sweep_job_is_not_a_results_job(tmp_path):
    c = TestClient(create_app(sweep_fn=lambda *a, **kw: engine.SweepStats(), search_fn=lambda q, **kw: fake_result(),
                              saved_path=tmp_path / "s.json"))
    jid = job_id(c.post("/sweeps", data=SWEEP).text)
    settle_sweep(c, jid)
    assert c.get(f"/jobs/{jid}/results").status_code == 404
    assert c.get(f"/jobs/{jid}/book/0").status_code == 404
    assert c.get(f"/sweeps/{start(c)}/progress").status_code == 404  # and a search job is not a sweep


def test_saved_search_with_legacy_cabin_key_still_runs(tmp_path):
    legacy = {"origins": "SJU", "destinations": "MAD", "date_from": "2026-12-01", "date_to": "2026-12-04",
              "cabin": "J", "max_searches": 3, "programs": [], "include_cash": False, "include_estimates": True,
              "refresh": False}
    path = tmp_path / "s.json"
    path.write_text(json.dumps([{"id": "abc12345", "name": "Old", "params": legacy}]), encoding="utf-8")
    c = TestClient(create_app(search_fn=lambda q, **kw: fake_result(), saved_path=path))
    assert "SJU to MAD, 2026-12-01..2026-12-04, J" in c.get("/").text
    run = c.post("/saved/abc12345/run")
    assert run.status_code == 200
    assert program_order(wait_done(c, job_id(run.text))) == ["AC", "AV", "KL"]


# --- shared-deployment guards (architecture.md M3-M7) ----------------------------------------

from flypoints import paths  # noqa: E402
from flypoints.cache import Cache  # noqa: E402
from flypoints.web import app as web_app  # noqa: E402
from flypoints.web.app import Limits  # noqa: E402


def test_max_searches_cap_rejects_live_search_sweep_and_saved_run(tmp_path):
    c = TestClient(create_app(search_fn=lambda q, **kw: fake_result(), saved_path=tmp_path / "s.json",
                              limits=Limits(max_searches=8, max_sweep_searches=40)))
    r = c.post("/jobs", data={**FORM, "max_searches": "500"})
    assert r.status_code == 422 and "at most 8 per search" in r.text
    assert c.post("/jobs", data={**FORM, "max_searches": "8"}).status_code == 200
    r = c.post("/sweeps", data={**SWEEP, "max_searches": "41"})
    assert r.status_code == 422 and "at most 40 per sweep" in r.text
    saved = c.post("/saved", data={**FORM, "max_searches": "500", "name": "big"}).text
    item_id = re.search(r"/saved/(\w+)/run", saved).group(1)
    assert c.post(f"/saved/{item_id}/run").status_code == 422
    assert 'max="8"' in c.get("/").text and 'max="40"' in c.get("/").text


def test_no_cap_by_default(client):
    assert client.post("/jobs", data={**FORM, "max_searches": "500"}).status_code == 200


def test_daily_budget_clamps_then_falls_back_to_stored(tmp_path):
    seen = []

    def live(query, **kw):
        seen.append(query.max_searches)
        return fake_result().model_copy(update={"live_searches_run": query.max_searches})

    def stored(query, **kw):
        return fake_result()

    cache = Cache(tmp_path / "c.sqlite")
    c = TestClient(create_app(cache=cache, search_fn=live, stored_fn=stored, saved_path=tmp_path / "s.json",
                              limits=Limits(daily_searches=5)))
    wait_done(c, start(c, max_searches="3"))
    html = wait_done(c, start(c, max_searches="3"))
    assert seen == [3, 2] and "cap 2" in html  # clamped to what was left
    html = wait_done(c, start(c, max_searches="3"))
    assert seen == [3, 2] and "budget (5) is used up" in html and "stored results only" in html
    assert "budget (5) is used up" in settle_sweep(c, job_id(c.post("/sweeps", data=SWEEP).text))
    assert web_app.DailyBudget(cache, 5).used() == 5  # survives a restart: kept in the cache table


def test_auth_message_replaces_provider_text(tmp_path):
    def fn(query, **kw):
        raise AuthError("Token expired: re-copy it.")

    c = TestClient(create_app(search_fn=fn, saved_path=tmp_path / "s.json",
                              limits=Limits(auth_message="Award search is unavailable; ask Jean.")))
    html = wait_done(c, start(c))
    assert "ask Jean" in html and "re-copy" not in html


def test_flights_column_shows_operating_flights(tmp_path):
    def seg(flight, a, b):
        return Segment(flight_number=flight, origin=a, destination=b, depart=NOW, arrive=NOW)

    o = AwardOffer(provider="t", program_code="AV", program_name="AV", origin="SJU", destination="MAD",
                   date=date(2026, 12, 2), cabin=Cabin.Y, miles=35000, taxes_usd=125.0,
                   segments=[seg("AV259", "SJU", "BOG"), seg("IB424", "BOG", "MAD")], fetched_at=NOW)
    result = engine.SearchResult(options=[build_ranked_option(o, None, load_programs(), load_bank_rules(), [])])
    c = TestClient(create_app(search_fn=lambda q, **kw: result, saved_path=tmp_path / "s.json"))
    html = wait_done(c, start(c, cabin="Y"))
    assert "<th>Flights</th>" in html and "AV259 · IB424" in html


def test_form_lists_programs_without_live_source(client):
    html = client.get("/").text
    assert "Not searched live (no data source): " in html and "IB" in html
    assert "IB Iberia Plus (Avios) (estimates only)" in html and "AC Air Canada Aeroplan (estimates only)" not in html


def test_serve_purges_expired_cache_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    cache = Cache()
    cache.put_json("ns", "old", 1, -1)
    cache.put_json("ns", "new", 2, 3600)
    cache.close()
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    web_app.serve()
    db = Cache(tmp_path / "cache.sqlite")
    assert db._db.execute("SELECT key FROM cache").fetchall() == [("new",)]
