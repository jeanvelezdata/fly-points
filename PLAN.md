# fly-points — research findings and implementation plan

Drafted 2026-10-19. Scope: search award redemptions reachable with **Capital One miles** and
**Chase Ultimate Rewards** (Sapphire/Ink holder, so airline transfers are available), with a
Python CLI, an MCP server, and a local web dashboard. Constraint: **free data sources only**.

---

## 1. Research findings: what data sources exist

### 1.1 Award availability (the hard part)

| Source | Official API? | Free? | Notes |
|---|---|---|---|
| **seats.aero** | Yes — Partner API (`Partner-Authorization` header, JSON, cached + live search, ~20 programs, 1,000 calls/day) | **No.** Key only with Pro ($9.99/mo or $99.99/yr), non-commercial use only; commercial use needs a written agreement. Free website tier limited to departures within ~60 days. | The best data source by far. Several community MCP servers already wrap it. |
| **PointsYeah** | **No public API.** Search parameters are encrypted client-side before hitting `api2.pointsyeah.com`. | Free account: unlimited searches, but 4-day window, 1 origin / 1 destination, 4 alerts. Premium $11.99/mo widens to 8 days and 2×2 airports. | Unofficial community MCP servers (`kevin0195/pointsyeahMCP`, `pointsyeah-mcp-server` on npm) reuse your logged-in Cognito refresh token + headless Playwright, then poll `fetch_result`. 30–90 s per search. Tokens expire; private endpoints may break; ToS not confirmed to allow it. Already maps Chase/Capital One → transfer partners. |
| **point.me** | **No.** `api.point.me` returns 401 everywhere; only private "Gateway" deals with banks (Amex, Bilt). | No | Not usable. |
| **Award Travel Finder** | API key + hosted MCP (`mcp.awardtravelfinder.com`) | Free: 3 economy searches/day. Paid $9.99/mo. | Too limited free; useful free **transfer-bonus tracker** page. |
| **Airline sites directly** | No public award APIs | Free, but scraping | Major airline sites sit behind Akamai/Cloudflare/DataDome; plain Python/headless requests get blocked at the TLS/fingerprint level. `lg/awardwiz` (archived Sep 2024, United/Delta broken) shows how costly upkeep is. |
| **Award Buddy** (`kevchentw/awardbuddy`, MIT) | n/a — browser extension/userscript | Free | Runs *inside your real logged-in browser tab* on the airline site, intercepts the site's own fetch/XHR. Covers Aeroplan, Flying Blue, LifeMiles, Cathay, EVA, JAL (all C1/Chase partners) plus AA, Alaska, Delta, ANA, Starlux. Avoids bot detection because it *is* a real browser session. Not headless. |

Legal context: Air Canada sued seats.aero under the CFAA over award scraping; the preliminary
injunction was denied (Mar 2024). Personal, low-volume use carries far less risk than that, but
every free path below is either unofficial or scraping. Treat it as best-effort.

**Bottom line:** no free, official award-availability API exists. The realistic free options are
(a) driving your own free PointsYeah account, (b) capturing results from airline sites in your
real browser, and (c) estimating prices from published award charts when no live data exists.

### 1.2 Cash fares (for cents-per-point)

- **Amadeus Self-Service**: shut down 2026-07-17. Gone.
- **Kiwi Tequila**: invite-only. **Skyscanner**: commercial only. **Google QPX**: dead since 2018.
- **`fast-flights`** (Python, reverse-engineered Google Flights protobuf): free, v3.x, fragile.
- **SerpApi Google Flights**: free 250 searches/month, then paid. Good fallback.
- **Ignav**: 1,000 free requests (vendor claim; not verified).

### 1.3 Transfer partners (verify against issuer pages before transferring)

**Chase UR** — all 1:1: Aer Lingus, Air Canada Aeroplan, Air France-KLM Flying Blue, British
Airways, Iberia, JetBlue, Singapore KrisFlyer, Southwest, United, Virgin Atlantic. (Emirates left
Oct 2025. Hyatt moved to 4:3 for Sapphire Preferred / likely Ink Preferred on 2026-10-01; hotel,
out of scope.)

**Capital One** — 1:1: Aeromexico, Aeroplan, Avianca LifeMiles, British Airways, Cathay, Etihad,
Finnair, Flying Blue, Qantas, Qatar, Singapore, TAP, Turkish, Virgin Red. Not 1:1: Emirates,
EVA, JAL at 4:3 (1,000 → 750); JetBlue at 5:3 (1,000 → 600).

Overlap (both banks): Aeroplan, Flying Blue, BA Avios, Singapore, JetBlue — the tool should show
which bank is cheaper given current bonuses.

Portal baselines for comparison: Capital One Travel ≈ 1.0¢/mile; Chase Travel Points Boost
(rate varies by card/booking, verify current terms).

### 1.4 Transfer bonuses

No RSS/API found. Free public trackers: awardtravelfinder.com/transfer-bonuses and
point.me/insights/current-transfer-bonuses. Plan: hand-maintained `bonuses.yaml` with an
optional scraper that proposes updates (diff for you to accept).

---

## 2. Recommendation

Build a **provider-agnostic core** with pluggable data sources, and start with:

1. **PointsYeah provider** (primary live source; covers both banks' partners; you already use it).
2. **Award-chart estimator** (offline; for chart-based programs like Aeroplan partner awards,
   Avios distance bands, LifeMiles, Turkish, Virgin Atlantic partners, KrisFlyer Saver).
   Labeled clearly as "estimated, availability unknown".
3. **fast-flights** for cash fares, SerpApi as fallback.
4. *(Later)* **Browser-capture provider**: a small userscript/extension (Award Buddy-style) that
   posts what you see on airline sites to the local dashboard, for programs PointsYeah misses.

One honest note: the free path is fragile — PointsYeah tokens expire and endpoints can change
without warning. If that maintenance becomes annoying, a seats.aero Pro key ($10/mo) drops into
the same provider interface with no other changes. I'm designing for that but not building it now.

---

## 3. Architecture

```
fly-points/
  pyproject.toml            # uv/pip; Python 3.11+
  data/
    programs.yaml           # bank -> partner, ratio, min transfer, transfer time, verified date
    bonuses.yaml            # active transfer bonuses with start/end dates
    regions.yaml            # "EUR", "JPN", "SJU-area" -> airport lists
    charts/*.yaml           # award charts for chart-based programs
  src/flypoints/
    models.py               # SearchQuery, AwardOffer, CashFare, RankedOption (pydantic)
    providers/
      base.py               # Provider protocol: search(query) -> list[AwardOffer]
      pointsyeah.py         # Cognito refresh -> Playwright create_task -> poll fetch_result
      charts.py             # offline estimates
      cash_fastflights.py
      cash_serpapi.py
    engine.py               # expand regions x dates, fan out, dedupe, rank
    pricing.py              # bank points needed (ratio, bonus), cpp vs cash and vs portal
    cache.py                # SQLite: raw responses + normalized offers, TTL per provider
    ratelimit.py            # per-provider concurrency + delay
    cli.py                  # Typer
    mcp_server.py           # FastMCP, stdio
    web/                    # FastAPI + Jinja2 + HTMX (no JS build step)
  tests/
```

All three interfaces are thin layers over `engine.search()`; no logic lives in them.

**Core calculation** per offer:
`bank_points = ceil(miles / ratio / (1 + bonus))` rounded up to the program's transfer
increment; `cpp = (cash_fare − award_taxes) / bank_points × 100`; compare against portal value.
Output ranks by bank points, then taxes, then cpp, and shows "cheaper via Chase / Capital One".

**Flexible search**: `SJU → EUR, 2027-03-01..2027-03-31, J` expands into origin×destination×date
windows. Because PointsYeah free returns a 4-day window per search, a month to 10 airports ≈
8 windows × 10 = 80 searches at 30–90 s each — so fan-out runs in the background with a cache,
progress bar, and a hard cap you set (default 40 searches per run).

---

## 4. Implementation plan

Each step has a check that must pass before moving on.

0. **Spike: confirm data sources still work (go/no-go).**
   - Log into PointsYeah (free), capture the refresh token, run one search with the community
     approach, save the raw JSON.
   - Run one `fast-flights` query (e.g. SJU→MAD).
   - *Check:* both return real data matching what the websites show. If PointsYeah fails,
     stop and revisit (browser-capture first, or reconsider seats.aero).
1. **Core models + data files.** `programs.yaml` (from §1.3, each with `verified:` date),
   `regions.yaml`, `bonuses.yaml`, `pricing.py`.
   - *Check:* unit tests for ratio math (4:3, 5:3, bonus, rounding to transfer increments) and
     cpp; known hand-calculated examples.
2. **PointsYeah provider + SQLite cache.** Parse raw JSON into `AwardOffer` using the spike
   fixture; token refresh; clear error when token expired ("re-copy token, see README").
   - *Check:* fixture-based parser tests; one live search returns same seats as the website.
3. **Cash providers.** fast-flights first, SerpApi fallback when env key present.
   - *Check:* live fare for one route within reason vs Google Flights in browser.
4. **Engine + CLI.** `flypoints search SJU EUR --dates 2027-03-01:2027-03-31 --cabin J
   --max-searches 40`; `flypoints programs`; `flypoints bonuses`.
   - *Check:* end-to-end run prints ranked table; second run hits cache (no network).
5. **Award-chart estimator** for chart-based programs; results flagged `estimated`.
   - *Check:* tests against published chart values for 5–10 known routes.
6. **MCP server** (FastMCP, stdio) exposing `search_awards`, `list_programs`,
   `transfer_bonuses`, `compare_cash`. Register in Claude Code via `claude mcp add`.
   - *Check:* Claude Code calls `search_awards` and gets the same results as the CLI.
7. **Web dashboard** (FastAPI + HTMX) on `localhost`: search form, sortable results, saved
   searches, background job progress.
   - *Check:* screenshot of a real search; filters/sorts work; long search doesn't block UI.
8. **Transfer bonus updater**: fetch public tracker page, propose diff to `bonuses.yaml`.
   - *Check:* run shows proposed changes; nothing written without confirmation.
9. *(Optional)* **Browser-capture userscript** for Aeroplan / Flying Blue / LifeMiles /
   Turkish pages → POST to `localhost` → stored as normal offers.

Steps 0–4 are the minimum usable tool. 6 and 7 are cheap once 4 exists.

---

## 5. Risks

- PointsYeah private endpoints/encryption change → provider breaks. Mitigation: fixture tests
  detect it immediately; provider interface lets another source take over.
- Token expiry is manual (copy from DevTools). Could be semi-automated with a persistent
  Playwright profile you log into once.
- Rate limits / account flagging on PointsYeah if fan-out is aggressive. Mitigation: caps,
  delays, cache.
- Partner ratios and bonuses change; `verified:` dates on every row and a warning when > 90 days.

## Sources

- seats.aero developer docs: https://developers.seats.aero/reference/getting-started-p
- PointsYeah MCP (unofficial): https://glama.ai/mcp/servers/p9o7n6k6ky
- point.me API status: https://supergood.ai/api-report-card/point-me , https://apis.io/providers/point-me/
- PointsYeah tiers: https://flightpoints.com/blogs/pointsyeah-vs-point-me/
- Award Buddy: https://github.com/kevchentw/awardbuddy
- AwardWiz (archived): https://github.com/lg/awardwiz
- Award Travel Finder: https://awardtravelfinder.com/seats-aero-alternative , https://awardtravelfinder.com/transfer-bonuses
- Amadeus shutdown: https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers , https://thunderbit.com/blog/best-flight-api-with-free-tiers
- Capital One partners: https://transferpoints.com/programs/capital-one , https://roamingcactus.com/credit-cards/capital-one-transfer-partners-2026
- Chase partners: https://thepointsking.com/blog/chase-transfer-partners/ , https://roamingcactus.com/credit-cards/chase-ur-transfer-partners-2026
- Air Canada v. seats.aero: https://en.wikipedia.org/wiki/Ian_Carroll_(software_developer)
