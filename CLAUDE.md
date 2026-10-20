# CLAUDE.md — fly-points

Award-flight search for Capital One miles + Chase Ultimate Rewards. Design and step list: `PLAN.md`.
This file records conventions and spike findings (2026-10-19) that override PLAN.md where they differ.

## Environment

- Python venv lives **outside OneDrive**: `C:/Users/jeanv/.venvs/flypoints`.
  - Python: `C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe`
  - Install: `uv pip install --python C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -e ".[dev]"`
  - Tests: `C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -m pytest -q`
- Never create `.venv/`, caches, or SQLite DBs inside this folder (OneDrive sync/locks).
- User state dir: `~/.flypoints/` (`C:/Users/jeanv/.flypoints`): `pointsyeah_token` (Cognito refresh
  token, one line; env `POINTSYEAH_REFRESH_TOKEN` overrides), `cache.sqlite`, `saved_searches.json`.
  Never print, log, or commit token contents.
- Layout is `src/flypoints/...` (src layout), package name `flypoints`, console script `flypoints`.
- Dependencies are fixed in `pyproject.toml`. Do not add new ones without saying so in your report.

## PointsYeah — what actually works (spike, 2026-10-19)

Pure HTTP, **no Playwright**. Reference implementation: `spikes/pointsyeah_spike.py`.

1. Cognito refresh: POST `https://cognito-idp.us-east-1.amazonaws.com/`, headers
   `Content-Type: application/x-amz-json-1.1`, `X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth`,
   body `{"AuthFlow":"REFRESH_TOKEN_AUTH","ClientId":"3im8jrentts1pguuouv5s57gfu","AuthParameters":{"REFRESH_TOKEN":...}}`
   → `AuthenticationResult.IdToken` (≈1 h). `NotAuthorizedException` = token expired → tell user to
   re-copy the `...refreshToken` cookie (HttpOnly; DevTools → Application → Cookies → pointsyeah.com).
2. create_task: POST `https://api2.pointsyeah.com/flight/search/create_task`, header `authorization: <IdToken>`
   (no Bearer) plus browser-like origin/referer/user-agent, body `{"data": enc(plain,"hJuaknzb"), "encrypted": enc(plain, jti[:8])}`.
   `enc` = AES-256-CBC, PKCS7, key = b64decode("LefjQ2pEXmiy/nNZvhJ43i8" + section + "YHYbn1hOuAgA="),
   IV = b"1020304050607080", output base64. `jti` comes from the IdToken payload.
   `plain` = compact JSON: search_type "one_way", cabins list, segments[{departure, arrival,
   departure_date:{from,to}}], passengers_v2 {adults, children}, source "mobile".
   A from..to window of 4 days worked on the free account.
3. fetch_result: POST `.../flight/search/fetch_result` `{"task_id": ...}`; server long-polls ~1–4 s.
   **Each snapshot carries only new results (deltas)** — accumulate across all snapshots until
   `data.status` in {"done","completed"}. SJU→MAD 4-day search: ~32 s, 1,481 routes.
4. Result shape: `data.result[]` = {program, code, date, departure, arrival, cost, routes[]};
   route = {payment{currency, tax(USD), miles, cabin, seats, cash_price}, segments[{flight_number,
   aircraft, dt, da, at, aa, layover, cabin, duration}], duration, program, code, url, transfer[{bank,
   points, actual_points, bonus_percentage, bonus_end_date, code}]}. Bank codes seen: "Chase",
   "Capital One", "Amex", "Bilt", "Citi"... Results include programs not reachable from Chase/C1
   (AA, AS, DL...). `seats` 9999 = unknown/plenty.
   Fixture (trimmed real response): `tests/fixtures/pointsyeah_sju_mad.json` = {"query", "snapshots":[...]}.

## fast-flights 3.1.0 (cash fares)

```python
from fast_flights import create_query, FlightQuery, Passengers, get_flights
q = create_query(flights=[FlightQuery(date="2026-12-02", from_airport="SJU", to_airport="MAD")],
                 seat="business", trip="one-way", passengers=Passengers(adults=1), currency="USD", language="en")
res = get_flights(q)   # iterable of Flights(type, price:int USD, airlines, flights=[SingleFlight(...)])
```
seat ∈ economy | premium-economy | business | first. Verified live: SJU→MAD 2026-12-02 economy $302, business $4,766.

## Conventions

- Cabin codes in our models: `Y`, `W`, `J`, `F` (map from/to provider strings at the provider edge).
- All money in USD floats; miles/points ints.
- Tests must not hit the network; live checks go behind `@pytest.mark.live` (deselected by default).
- CLI, MCP server and web app are thin layers over `flypoints.engine.search()`; no logic in them.
