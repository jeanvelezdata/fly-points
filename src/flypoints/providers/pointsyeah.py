"""PointsYeah award provider: Cognito refresh -> AES-encrypted create_task -> poll fetch_result.

See CLAUDE.md for the protocol notes. Never put token text in logs or exception messages.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import date, datetime, timezone
from typing import Callable

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from flypoints import config, paths
from flypoints.cache import Cache, make_key
from flypoints.models import AwardOffer, Cabin, Segment
from flypoints.providers.base import AuthError, ProviderError

CLIENT_ID = "3im8jrentts1pguuouv5s57gfu"
COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
API = "https://api2.pointsyeah.com"
KEY_PREFIX, KEY_SUFFIX, DEFAULT_SECTION = "LefjQ2pEXmiy/nNZvhJ43i8", "YHYbn1hOuAgA=", "hJuaknzb"
IV = b"1020304050607080"
HEADERS = {
    "content-type": "application/json",
    "origin": "https://www.pointsyeah.com",
    "referer": "https://www.pointsyeah.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
}

# Engine-side rate limits for this provider (use with flypoints.ratelimit.RateLimiter).
MIN_INTERVAL_S = 3.0
MAX_CONCURRENT = 2

RAW_NAMESPACE = "pointsyeah_raw"
RAW_TTL_S = 24 * 3600
TOKEN_ENV = "POINTSYEAH_REFRESH_TOKEN"
REFRESH_MARGIN_S = 300
POLL_FLOOR_S = 0.3
DONE_STATUSES = {"done", "completed"}

_TOKEN_HELP = (
    "PointsYeah refresh token {problem}. In a browser logged in to pointsyeah.com open DevTools -> "
    "Application -> Cookies -> https://www.pointsyeah.com, filter for \"refresh\", copy the value of "
    "the CognitoIdentityServiceProvider...refreshToken cookie and save it (one line) to "
    f"~/.flypoints/pointsyeah_token, or set {TOKEN_ENV}."
)


def encrypt(plain: str, section: str) -> str:
    """AES-256-CBC / PKCS7 / base64, key derived from ``section``."""
    key = base64.b64decode(KEY_PREFIX + section + KEY_SUFFIX)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def jwt_payload(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _merge_snapshots(snapshots: list[dict]) -> list[dict]:
    """Flatten snapshots into one list of result entries.

    Evidence (16-snapshot SJU->MAD 4-day search, 1,481 routes): snapshots are deltas. No
    (code, date, departure, arrival) key appeared in more than one snapshot and no route was
    repeated, so entries are simply accumulated. As a guard (the community client does the
    same), if a key ever does repeat, the entry with more routes wins.
    """
    merged: dict[tuple, dict] = {}
    for snap in snapshots:
        for entry in (snap.get("data") or {}).get("result") or []:
            key = (entry.get("code"), entry.get("date"), entry.get("departure"), entry.get("arrival"))
            old = merged.get(key)
            if old is None or len(entry.get("routes") or []) > len(old.get("routes") or []):
                merged[key] = entry
    return list(merged.values())


def _segment_cabin(name: str | None) -> Cabin | None:
    """Segment cabins can be fare-brand names (Delta: "Main", "Main Basic", "Comfort") -> None."""
    try:
        return Cabin.from_provider(name) if name else None
    except ValueError:
        return None


def _parse_route(entry: dict, route: dict, programs: dict, fetched_at: datetime) -> AwardOffer:
    pay = route["payment"]
    seats = int(pay["seats"]) if pay.get("seats") is not None else 9999  # UA sends "9999" as str
    segments = [
        Segment(
            flight_number=s["flight_number"],
            origin=s["da"],
            destination=s["aa"],
            depart=datetime.fromisoformat(s["dt"]),
            arrive=datetime.fromisoformat(s["at"]),
            aircraft=s.get("aircraft") or None,
            cabin=_segment_cabin(s.get("cabin")),
        )
        for s in route["segments"]
    ]
    return AwardOffer(
        provider="pointsyeah",
        program_code=config.resolve_code(route.get("code") or entry["code"], programs),
        program_name=route.get("program") or entry.get("program") or entry["code"],
        origin=entry["departure"],
        destination=entry["arrival"],
        date=date.fromisoformat(entry["date"]),
        cabin=Cabin.from_provider(pay["cabin"]),
        miles=int(pay["miles"]),
        taxes_usd=float(pay["tax"]),
        seats=None if seats == 9999 else seats,
        segments=segments,
        booking_url=route.get("url") or None,
        fetched_at=fetched_at,
    )


def _parse(snapshots: list[dict], fetched_at: datetime) -> tuple[list[AwardOffer], int]:
    programs = config.load_programs()
    offers, non_usd = [], 0
    for entry in _merge_snapshots(snapshots):
        for route in entry.get("routes") or []:
            try:
                if (route["payment"].get("currency") or "USD") != "USD":
                    non_usd += 1
                    continue
                offers.append(_parse_route(entry, route, programs, fetched_at))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
    return offers, non_usd


def parse_results(snapshots: list[dict], fetched_at: datetime) -> list[AwardOffer]:
    """Snapshots from fetch_result -> offers (all programs, all cabins).

    Malformed routes are skipped; routes priced in a non-USD currency are skipped too.
    """
    return _parse(snapshots, fetched_at)[0]


def _json_or_empty(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class PointsYeahProvider:
    name = "pointsyeah"
    max_window_days = 4

    def __init__(
        self,
        client: httpx.Client | None = None,
        cache: Cache | None = None,
        timeout_s: float = 150,
        poll_floor_s: float = POLL_FLOOR_S,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), headers=HEADERS)
        self._cache = cache
        self.timeout_s = timeout_s
        self.poll_floor_s = poll_floor_s
        self._sleep = sleep
        self.skipped_non_usd = 0  # routes dropped by the most recent search()
        self._id_token: str | None = None
        self._jti = ""
        self._exp = 0.0

    # --- auth -----------------------------------------------------------------------------

    def _refresh_token(self) -> str:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            f = paths.STATE_DIR / "pointsyeah_token"
            if f.is_file():
                token = f.read_text().strip()
        if not token:
            raise AuthError(_TOKEN_HELP.format(problem="is missing"))
        return token

    def _auth_headers(self) -> dict:
        if self._id_token is None or time.time() > self._exp - REFRESH_MARGIN_S:
            self._login()
        return {**HEADERS, "authorization": self._id_token}

    def _login(self) -> None:
        body = {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {"REFRESH_TOKEN": self._refresh_token()},
        }
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        }
        try:
            r = self._client.post(COGNITO_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"PointsYeah login failed: {type(e).__name__}") from None
        if r.status_code != 200:
            kind = _json_or_empty(r).get("__type", "")
            if "NotAuthorizedException" in kind:
                raise AuthError(_TOKEN_HELP.format(problem="has expired or was rejected"))
            raise ProviderError(f"PointsYeah login failed (HTTP {r.status_code} {kind})".strip())
        try:
            token = r.json()["AuthenticationResult"]["IdToken"]
            payload = jwt_payload(token)
            self._jti, self._exp = payload["jti"], float(payload["exp"])
        except (KeyError, ValueError, IndexError):
            raise ProviderError("PointsYeah login returned an unexpected response") from None
        self._id_token = token

    # --- search ---------------------------------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self._client.post(f"{API}/flight/search/{path}", headers=self._auth_headers(), json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"PointsYeah {path} failed: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            self._id_token = None
            raise AuthError(_TOKEN_HELP.format(problem="was rejected by the search API"))
        if r.status_code != 200:
            raise ProviderError(f"PointsYeah {path} failed (HTTP {r.status_code})")
        data = _json_or_empty(r)
        if not data:
            raise ProviderError(f"PointsYeah {path} returned an unexpected response")
        return data

    def search(
        self, origin: str, destination: str, date_from: date, date_to: date, cabin: Cabin
    ) -> list[AwardOffer]:
        if (date_to - date_from).days + 1 > self.max_window_days:
            raise ProviderError(f"PointsYeah accepts at most {self.max_window_days} days per search")
        plain = json.dumps(
            {
                "search_type": "one_way",
                "cabins": [cabin.to_provider()],
                "segments": [
                    {
                        "arrival": destination,
                        "departure": origin,
                        "departure_date": {"from": date_from.isoformat(), "to": date_to.isoformat()},
                    }
                ],
                "passengers_v2": {"adults": 1, "children": 0},
                "source": "mobile",
            },
            separators=(",", ":"),
        )
        self._auth_headers()  # ensure the IdToken (and self._jti) is fresh
        created = self._post(
            "create_task",
            {"data": encrypt(plain, DEFAULT_SECTION), "encrypted": encrypt(plain, self._jti[:8])},
        )
        try:
            task_id = created["data"]["task_id"]
        except (KeyError, TypeError):
            raise ProviderError("PointsYeah create_task returned no task_id") from None

        snapshots: list[dict] = []
        deadline = time.monotonic() + self.timeout_s
        while True:
            snap = self._post("fetch_result", {"task_id": task_id})
            snapshots.append(snap)
            if (snap.get("data") or {}).get("status") in DONE_STATUSES:
                break
            if time.monotonic() >= deadline:
                raise ProviderError(f"PointsYeah search did not finish within {self.timeout_s:.0f}s")
            self._sleep(self.poll_floor_s)

        if self._cache is not None:
            key = make_key(origin, destination, date_from.isoformat(), date_to.isoformat(), cabin.value)
            self._cache.put_json(RAW_NAMESPACE, key, snapshots, RAW_TTL_S)
        offers, self.skipped_non_usd = _parse(snapshots, datetime.now(timezone.utc))
        return offers
