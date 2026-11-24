# fly-points

Search award flights you can book with **Chase Ultimate Rewards** or **Capital One miles**, ranked
by bank points needed, with cash fares and cents-per-point. Free data sources only: PointsYeah
(your free account), Google Flights via `fast-flights`, and published award charts (estimates).

## Setup

```powershell
uv venv --python C:/Users/jeanv/miniforge3/python.exe C:/Users/jeanv/.venvs/flypoints
uv pip install --python C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -e ".[dev]"
```

The venv lives outside OneDrive on purpose. Run commands with
`C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -m flypoints ...` (or `flypoints ...` with the venv activated).

### PointsYeah token (expires every ~30–90 days)

1. Log in at https://www.pointsyeah.com.
2. DevTools (F12) → **Application** → **Cookies** → `https://www.pointsyeah.com` → filter `refresh`.
3. Copy the value of `CognitoIdentityServiceProvider.<id>.<user>.refreshToken` (it's HttpOnly, so
   `document.cookie` won't show it).
4. Save it as the only line of `C:\Users\jeanv\.flypoints\pointsyeah_token`
   (or set `POINTSYEAH_REFRESH_TOKEN`).

When it expires, searches stop with an "expired" message — repeat the steps.

## Use

```powershell
flypoints search SJU EUR --dates 2027-03-01:2027-03-31 --cabin J --max-searches 40
flypoints search SJU MAD,LIS --dates 2027-03-05 --cabin J --program AC,AV --json
flypoints search SJU EUR --dates 2027-03-01:2027-03-31 --cabin Y,J --direct --days mon,fri --bank chase
flypoints search SJU EUR --dates 2027-03-01:2027-03-31 --stored --view compare   # instant, no live calls
flypoints search SJU MAD --dates 2027-03-05 --book 1   # also print how to book result #1
flypoints sweep SJU EUR --dates 2027-01-01:2027-12-15 --cabin J --max-searches 40
flypoints programs          # transfer ratios + verified dates
flypoints bonuses           # active transfer bonuses (data/bonuses.yaml)
flypoints bonuses-update    # scrape public trackers, show a diff, write only if you confirm
flypoints web               # dashboard at http://127.0.0.1:8765
flypoints web --reload      # same, restarting on code changes (for development)
flypoints mcp               # MCP server on stdio
```

Origins/destinations take IATA codes or region names from `data/regions.yaml` (EUR, JPN, SJU-area,
USA-hubs, SAM). Each live PointsYeah search covers one airport pair × 4 days and takes ~30–40 s,
so a month to 10 airports is ~80 searches; `--max-searches` caps the uncached ones (default 40).
Results are cached in `~/.flypoints/cache.sqlite` (awards 6 h, cash 12 h); `--refresh` bypasses it.
Live results are also kept for 30 days in `~/.flypoints/offers.sqlite`, which is what `--stored`
and `sweep` use. Dates may be up to 365 days ahead.

### `search` flags

- `--cabin Y,J` (comma-separated or repeated; `Y` economy, `W` premium economy, `J` business, `F` first).
- Filters, all optional: `--direct`, `--max-stops N`, `--carriers AC,UA` (operating carrier of every
  segment), `--days mon,fri` (departure weekday), `--max-taxes USD`, `--max-points N` (best bank),
  `--min-seats N` (unknown seat counts pass), `--bank chase|capital_one` (comma-separated or
  repeated: only offers that bank can reach), `--min-cabin-pct N` (minimum % of the journey flown in
  the offer's cabin). Carrier and stop filters drop award-chart estimates, which have no routing.
- `--stored` answers from the local store only (no live search, no cash lookups); `--max-age-hours`
  (default 24) is the oldest search it will use, and a warning says how much of the range is covered.
- `--view list|compare`. `compare` shows one row per program/route/date with Y/W/J/F columns: the
  cheapest option per cabin as best-bank points, miles and seats (`~` marks an estimate).
- The list view numbers its rows (`#`). `--book N` prints the booking plan for result `N` of that
  list as a panel: ordered steps with links, alternatives, and the data that is unknown.
- List columns: **Seen** is the age of the data (`3h ago`; `-` for estimates). The cabin shows `J*
  (72% J)` when part of the journey is in a lower cabin, and `J?` when a live offer's cabin mix is
  unknown.

### `sweep`

`flypoints sweep ORIGINS DESTS --dates FROM:TO --cabin J --max-searches 40 --fresh-hours 24` runs
live searches only to fill the local store (nothing is ranked). Windows searched within
`--fresh-hours` are skipped. Ctrl-C stops after the searches in flight and prints the statistics
(planned, already fresh, over the cap, run, errors). Afterwards `search --stored` answers instantly.

### Web dashboard

`flypoints web`, then open http://127.0.0.1:8765.

- The search form has multi-select cabins and the filters above. **Search** runs a live search as a
  background job; **Search stored (instant)** reads only the local store.
- Results can be switched between **List** and **Compare** (the grid above) and filtered further
  by program, stops, carriers, days, taxes, points, seats and minimum cabin %. The **Seen** column
  and a mixed-cabin badge (hover for the percentage; `?` = unknown) are in both views.
- **How to book** on a row (or a compare cell) opens the booking panel for that offer, estimates
  included. The existing **book** link stays when the provider supplied a URL.
- The **Sweep** section starts a sweep as a background job (it shares the one-at-a-time queue with
  live searches), shows progress, has a **Stop** button, and ends with the sweep statistics.

### MCP tools

`search_awards` takes `cabins` (list), the filters (`direct_only`, `max_stops`, `carriers`, `days`,
`max_taxes_usd`, `max_points`, `min_seats`, `banks`, `min_cabin_pct`), and `stored` / `max_age_hours`.
`booking_plan` takes the same arguments plus `index` (the option's 1-based position in the
`search_awards` result) and returns the steps, alternatives and unknowns; it repeats the search, so
it is instant when that search is cached or stored. `sweep` fills the store (default
`max_searches` 8). The older tools (`list_programs`, `transfer_bonuses`, `compare_cash`,
`list_regions`) are unchanged.

Claude Code: `claude mcp add fly-points -- C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -m flypoints.mcp_server`

## Reading the results

- **Best bank pts** includes ratios (e.g. Capital One → JAL 4:3), active bonuses and each bank's
  transfer increment (Chase 1,000; Capital One 100).
- **Cash / cpp**: cash is the cheapest Google Flights fare in that cabin for that route and date,
  not necessarily the same itinerary, so treat cpp as a guide.
- **EST** rows come from award charts (Aeroplan partner, Turkish) with great-circle distance and
  rough taxes. Availability is unknown, and connecting itineraries can cost more.
- Ratios in `data/programs.yaml` were verified 2026-10-19. Confirm on the bank's site before
  transferring, because transfers can't be undone.

## Booking plans and their data caveats

A booking plan (CLI `--book`, web "How to book", MCP `booking_plan`) lists, in order: verify the
award seat, transfer from each bank that reaches the program (best value first, with the current
bonus and transfer increments), a warning that transfers are irreversible, then how to book, plus
program-specific gotchas and other Chase / Capital One programs in the same alliance.

The per-program facts come from `data/booking.yaml`, each with sources and a verified date. Many
fields are `null` or `unknown` on purpose: phone numbers, hold policies, transfer times, whether
partner awards can be booked online, and most deep links. Those are never guessed. The plan prints
what is missing under "unknown" instead, and falls back to the program's award-search page when
there is no verified deep link. Estimated offers get a leading warning that availability is unknown.
Alternatives are programs in the same alliance as the operating carriers; whether they actually
show the same space still has to be checked before transferring.

## Tests

`python -m pytest -q` (offline). `python -m pytest -m live` hits the real services.
