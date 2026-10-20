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

## Tests

`python -m pytest -q` (offline). `python -m pytest -m live` hits the real services.
