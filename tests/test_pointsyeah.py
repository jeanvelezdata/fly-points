import base64
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from flypoints.cache import Cache
from flypoints.models import Cabin
from flypoints.providers import pointsyeah as py
from flypoints.providers.base import AuthError, ProviderError

FIXTURE = Path(__file__).parent / "fixtures" / "pointsyeah_sju_mad.json"
NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
SECRET = "SECRET-REFRESH-TOKEN-123"
CREATE = f"{py.API}/flight/search/create_task"
FETCH = f"{py.API}/flight/search/fetch_result"


@pytest.fixture(scope="module")
def snapshots():
    return json.loads(FIXTURE.read_text())["snapshots"]


@pytest.fixture(scope="module")
def offers(snapshots):
    return py.parse_results(snapshots, NOW)


def _id_token(exp=None, jti="abcdefgh-1234"):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    exp = exp if exp is not None else int(time.time()) + 3600
    return f"{b64({'alg': 'none'})}.{b64({'jti': jti, 'exp': exp})}.sig"


def _decrypt(b64text, section):
    key = base64.b64decode(py.KEY_PREFIX + section + py.KEY_SUFFIX)
    dec = Cipher(algorithms.AES(key), modes.CBC(py.IV)).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    data = dec.update(base64.b64decode(b64text)) + dec.finalize()
    return (unpadder.update(data) + unpadder.finalize()).decode()


def _cognito_ok(id_token):
    return httpx.Response(200, json={"AuthenticationResult": {"IdToken": id_token}})


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv(py.TOKEN_ENV, SECRET)


def _provider(**kw):
    kw.setdefault("poll_floor_s", 0)
    return py.PointsYeahProvider(**kw)


# --- parser ---------------------------------------------------------------------------------


def test_parse_fixture(offers):
    assert len(offers) == 79
    assert {o.provider for o in offers} == {"pointsyeah"}


def test_known_avianca_business_route(offers):
    hits = [
        o
        for o in offers
        if o.program_code == "AV"
        and o.cabin is Cabin.J
        and o.date == date(2026, 12, 2)
        and o.miles == 51310
    ]
    assert len(hits) == 1
    o = hits[0]
    assert o.taxes_usd == 10.6
    assert o.seats == 4
    assert (o.origin, o.destination) == ("SJU", "MAD")
    assert [s.flight_number for s in o.segments] == ["AV259", "AV10"]
    assert o.segments[0].depart == datetime(2026, 12, 2, 18, 20)
    assert o.segments[0].cabin is Cabin.J
    assert o.program_name == "Avianca LifeMiles"
    assert o.booking_url is None  # LifeMiles routes carry an empty url


def test_seats_9999_is_none_and_cabins_mapped(offers):
    assert any(o.seats is None for o in offers)
    assert all(o.seats != 9999 for o in offers)
    assert {o.cabin for o in offers} >= {Cabin.Y, Cabin.J}


def test_malformed_and_non_usd_routes_skipped(snapshots):
    entry = next(e for s in snapshots for e in s["data"]["result"] if e["routes"])
    good = entry["routes"][0]
    eur = json.loads(json.dumps(good))
    eur["payment"]["currency"] = "EUR"
    broken = {"payment": {"miles": 1}}
    snap = {"data": {"result": [{**entry, "routes": [good, eur, broken]}]}}
    assert len(py.parse_results([snap], NOW)) == 1
    assert py._parse([snap], NOW)[1] == 1


def test_merge_keeps_entry_with_more_routes():
    a = {"code": "AV", "date": "2026-12-01", "departure": "SJU", "arrival": "MAD", "routes": [1]}
    b = {**a, "routes": [1, 2]}
    merged = py._merge_snapshots([{"data": {"result": [a]}}, {"data": {"result": [b]}}])
    assert merged == [b]


# --- encryption -----------------------------------------------------------------------------


def test_encrypt_round_trips():
    plain = '{"search_type":"one_way"}'
    assert _decrypt(py.encrypt(plain, py.DEFAULT_SECTION), py.DEFAULT_SECTION) == plain
    assert _decrypt(py.encrypt(plain, "abcdefgh"), "abcdefgh") == plain


# --- auth -----------------------------------------------------------------------------------


def test_missing_token_is_auth_error_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv(py.TOKEN_ENV, raising=False)
    monkeypatch.setattr(py.paths, "STATE_DIR", tmp_path)
    with pytest.raises(AuthError) as e:
        _provider().search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J)
    assert "pointsyeah_token" in str(e.value)
    assert "DevTools" in str(e.value)


@respx.mock
def test_not_authorized_maps_to_auth_error_without_token(token_env):
    respx.post(py.COGNITO_URL).respond(400, json={"__type": "NotAuthorizedException", "message": "x"})
    with pytest.raises(AuthError) as e:
        _provider().search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J)
    assert "expired" in str(e.value)
    assert SECRET not in str(e.value)


@respx.mock
def test_other_cognito_failure_is_provider_error(token_env):
    respx.post(py.COGNITO_URL).respond(500, json={"__type": "InternalErrorException"})
    with pytest.raises(ProviderError) as e:
        _provider().search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J)
    assert not isinstance(e.value, AuthError)
    assert SECRET not in str(e.value)


@respx.mock
def test_id_token_cached_and_refreshed_near_expiry(token_env):
    cognito = respx.post(py.COGNITO_URL)
    cognito.side_effect = [_cognito_ok(_id_token(exp=int(time.time()) + 3600))]
    p = _provider()
    p._auth_headers()
    p._auth_headers()
    assert cognito.call_count == 1
    p._exp = time.time() + 100  # inside the 5 minute margin
    cognito.side_effect = [_cognito_ok(_id_token())]
    p._auth_headers()
    assert cognito.call_count == 2


# --- poll loop ------------------------------------------------------------------------------


def _snap(status, results=()):
    return httpx.Response(200, json={"code": 0, "success": True, "data": {"status": status, "result": list(results)}})


@respx.mock
def test_search_polls_until_done_and_caches_raw(token_env, snapshots, tmp_path):
    entries = [e for s in snapshots for e in s["data"]["result"] if e["routes"]][:2]
    respx.post(py.COGNITO_URL).mock(return_value=_cognito_ok(_id_token(jti="abcdefgh-1234")))
    create = respx.post(CREATE).respond(200, json={"data": {"task_id": "T1"}})
    fetch = respx.post(FETCH)
    fetch.side_effect = [_snap("processing", entries[:1]), _snap("processing", entries[1:]), _snap("done")]
    cache = Cache(tmp_path / "c.sqlite")
    p = _provider(cache=cache)

    offers = p.search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J)

    assert fetch.call_count == 3
    assert len(offers) == sum(len(e["routes"]) for e in entries)
    # request body: only the requested cabin, both encrypted fields decrypt to the same plain text
    sent = json.loads(create.calls[0].request.content)
    plain = _decrypt(sent["data"], py.DEFAULT_SECTION)
    assert _decrypt(sent["encrypted"], "abcdefgh") == plain
    q = json.loads(plain)
    assert q["cabins"] == ["Business"] and q["search_type"] == "one_way"
    assert q["segments"][0]["departure_date"] == {"from": "2026-12-01", "to": "2026-12-04"}
    assert q["passengers_v2"] == {"adults": 1, "children": 0}
    assert create.calls[0].request.headers["authorization"].count(".") == 2  # raw IdToken, no Bearer
    key = py.make_key("SJU", "MAD", "2026-12-01", "2026-12-04", "J")
    assert len(cache.get_json(py.RAW_NAMESPACE, key)) == 3


@respx.mock
def test_poll_timeout_is_provider_error(token_env):
    respx.post(py.COGNITO_URL).mock(return_value=_cognito_ok(_id_token()))
    respx.post(CREATE).respond(200, json={"data": {"task_id": "T1"}})
    respx.post(FETCH).mock(return_value=_snap("processing"))
    with pytest.raises(ProviderError, match="did not finish"):
        _provider(timeout_s=0).search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J)


def test_window_too_long(token_env):
    with pytest.raises(ProviderError):
        _provider().search("SJU", "MAD", date(2026, 12, 1), date(2026, 12, 5), Cabin.J)


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_search():
    offers = py.PointsYeahProvider(cache=Cache()).search(
        "SJU", "MAD", date(2026, 12, 1), date(2026, 12, 4), Cabin.J
    )
    assert any(o.cabin is Cabin.J for o in offers)
