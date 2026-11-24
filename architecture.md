# fly-points: deployment architecture (friends edition)

Drafted 2026-11-24. Status: **deployed 2026-11-24** at `https://<VM IP with dashes>.sslip.io/`
(user `friends`; the password is in `/root/flypoints-shared-password` on the VM, root-only). It
covers how the web app is hosted for a handful of friends, the code changes made first, and the
Iberia coverage finding that came up while checking the 2027-01-28..02-02 SJU→MAD search.

Deviations from the runbook below, as built:

- uv comes from PyPI into `/opt/uv` (`/opt/uv/bin/uv`), not from the install script.
- The Caddyfile has no file `log` block: Caddy failed to start with one. Errors go to journald.
- **Cash fares on the VM** first failed: it is in Finland, Google served its EU consent page, and
  fast-flights couldn't parse it. Fixed the same day. `providers/cash_fastflights.py` now sends
  `SOCS=CAI`, Google's "reject all" consent state, through fast-flights' `FetchIntegration` hook.
  If Google changes that cookie, the same "fast-flights failed" errors will come back.

## 1. Decisions

| Question | Decision | Consequence |
|---|---|---|
| Where it runs | The existing Hetzner VM (Debian 12, 2 vCPU / 4 GB), alongside ig-agent | $0 extra; must not weaken ig-agent's isolation (§4) |
| PointsYeah credentials | One shared refresh token (Jean's account) | Every friend's search counts against Jean's account; needs a global search budget (M3) |
| Login | One shared password | Enforced by Caddy `basic_auth`; no auth code in the app; no per-user data |
| Ingress | Caddy on 80/443 with a Let's Encrypt cert | Opens 80/443 in `ufw` on a VM that today exposes only SSH |

Out of scope: the MCP server and CLI stay local-only; no per-user accounts, quotas or saved
searches (everyone shares one saved-search list).

## 2. Iberia: what was found

**Configured, never searched live.** `IB` is in `data/programs.yaml` (Chase 1:1), but no
provider returns Iberia Plus prices:

- PointsYeah never returned an `IB` program entry, or `BA`, `QR`, `AY` or `EI`, in any cached
  search (105 result sets, SJU→MAD and SJU↔EZE, 2026-10-19 to 2026-11-23). It also returned
  none in two live re-runs of 2027-01-28..31 on 2026-11-24, with `"source": "mobile"` and with
  `"web"`. Both returned the same 19 programs.
- pointsyeah.com's own Live Search program list (checked 2026-11-24, logged in) has no British
  Airways, Iberia, Qatar, Finnair, Aer Lingus, Singapore, Cathay, Etihad, TAP, EVA or JAL. **This
  is a PointsYeah coverage gap, not a bug in our request or filters.**
- The chart estimator has only `AC.yaml` and `TK.yaml`, so it covers no Avios program either.

**Iberia-operated flights were in the results, under another program's name.** The 2027-01-28..31
re-run produced 17 Iberia-operated economy routes. 13 were priced through Avianca LifeMiles
(Capital One 1:1); the AA- and Alaska-priced ones were dropped because no bank reaches them. They
include the **IB382 nonstop on 2027-01-28 at 35,000 LifeMiles + $125**. All 13 survive
`_filter_and_dedupe` and pricing. But the results table shows only the program code and the airport
route (`views.option_route`), so that row read "AV · SJU-MAD". Nothing on screen said Iberia. No
Iberia-operated premium-cabin space appeared through any program.

**Reachable programs with no live source** (11 of 22 in `programs.yaml`): EI, BA, IB, SQ, CX, EY,
AY, QR, TP, BR, JL. The UI currently says nothing about this.

Fixes, in order of value per effort:

1. **Show the operating flights** (M5). This alone would have surfaced IB382 yesterday.
2. **Say which programs aren't searched live** (M6), so an empty result for Iberia isn't read as
   "no space".
3. **Iberia chart estimate** (optional). Iberia publishes distance-based Avios prices for
   Iberia-operated flights. `data/charts/IB.yaml` would give estimate rows, with price but no seat
   availability, through the existing `ChartEstimator`. `data/charts/README.md` currently says
   BA/IB have no chart: verify Iberia's current table before writing numbers.
4. **A live Avios source via seats.aero.** Researched 2026-11-24 from seats.aero's developer docs
   and terms; nothing has been tested with a key yet.
   - **Price and access:** Pro is $12.99/mo or $109.99/yr since 2026-11-09. The Partner API is
     cached search only: live search is not available to Pro users. 1,000 calls/day.
   - **Covered by the API today:** QR (`qatar`), AY (`finnair`), SQ (`singapore`) and EY
     (`etihad`). Qatar and Singapore rows come without taxes or seat counts.
   - **IB and BA:** added to the seats.aero website in beta (2026), but they're **not listed as
     API sources**. Whether the API serves them, and whether SJU–MAD is covered, needs one
     `GET /routes?source=iberia` call with a key.
   - **Not covered at all:** EI, CX, TP, BR, JL.
   - **Terms:** non-commercial use is allowed with visible attribution. Flights more than 60 days
     out must not be shown to third parties unless they sign in through seats.aero's OAuth (each
     friend with their own Pro account) or there's a commercial agreement. Friends behind a
     password are still third parties, so either limit seats.aero rows to 60 days for friends or
     ask support@seats.aero.
   - **Integration sketch:**
     - a new `providers/seatsaero.py`, with taxes and segments coming from the trips endpoint;
     - a per-provider rate limit in `engine._run_searches`, which currently gives every non-chart
       provider PointsYeah's limits;
     - a `seatsaero:` source name per program in `programs.yaml`;
     - its own counter in the M3 daily budget.

   Alternatives: Award Travel Finder ($49/mo, MCP-only; names BA/IB/QR/CX/EY, with estimated
   taxes) and AwardWallet's award-search API (pricing on request). Scraping iberia.com directly is
   not recommended: it is bot-protected and fragile.

## 3. Target architecture

```
friend's browser
   │  https://<host>/            (shared password prompt)
   ▼
Hetzner VM ─ ufw: 22 (rate-limited), 80, 443
   │
   ├─ caddy.service             :80 → redirect, :443 TLS (Let's Encrypt)
   │     basic_auth (bcrypt)    only the fly-points hostname is served; bare-IP requests get nothing
   │     reverse_proxy 127.0.0.1:8765
   │
   ├─ flypoints.service         user `flypoints`, HOME=/var/lib/flypoints
   │     uvicorn, 1 process      (JobRunner and jobs live in memory; must stay single-process)
   │     ├─ /srv/flypoints       git checkout, editable install, data/*.yaml
   │     ├─ /var/lib/flypoints/.flypoints/{cache.sqlite, offers.sqlite, saved_searches.json}
   │     └─ /etc/flypoints/env   POINTSYEAH_REFRESH_TOKEN, SERPAPI key (optional); 0640 root:flypoints
   │           outbound → cognito-idp / api2.pointsyeah.com / Google Flights (fast-flights) / SerpApi
   │
   └─ (unchanged) ig-review.service 127.0.0.1:8420 ← cloudflared tunnel ← Cloudflare Access
                  other loopback listeners: 8100, 20241
```

Why this shape:

- **Auth at Caddy, not in the app.** With one shared password, the app has no use for identity.
  `basic_auth` keeps auth out of the code and means uvicorn is never reachable unauthenticated,
  because it binds loopback only.
- **Single uvicorn process.** `web/app.py`'s `JobRunner` keeps jobs in memory and drains one FIFO
  queue on one thread, which already serializes PointsYeah use. More workers would break job
  lookup and multiply load on the shared token.
- **No code changes for state paths.** `paths.STATE_DIR` is `Path.home() / ".flypoints"`, so
  running as a system user whose home is `/var/lib/flypoints` puts all state there. The token
  comes from `POINTSYEAH_REFRESH_TOKEN`, which `pointsyeah.py` already reads ahead of the token
  file.
- **Editable install is required.** `paths.DATA_DIR` is `parents[2] / "data"` relative to the
  package source. A non-editable wheel install would look for `data/` inside site-packages and fail.

## 4. Sharing PRISMA's production VM safely

This VM holds ig-agent's live Instagram token (`/etc/ig-agent/env`) and was deliberately built
with **no public listener** (ig-agent `ARCHITECTURE.md` §14.10). Opening 443 changes that, so:

- fly-points runs as its own system user `flypoints`, with no sudo, outside the `ig` group, and
  unable to read `/etc/ig-agent` or `/srv/ig-*`.
- systemd hardening on `flypoints.service`: `ProtectSystem=strict`, `ReadWritePaths=/var/lib/flypoints`,
  `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`, `MemoryMax=1G`, `CPUWeight=50`. The
  last two keep a runaway search from starving ig-agent's morning timers.
- Caddy serves only the fly-points site block. It never proxies 8420 or 8100, and ig-review keeps
  its tunnel-only path.
- Check whether a Hetzner Cloud Firewall is attached to the server in the Hetzner console. If one
  is, 80/443 must be allowed there too, not only in `ufw`.
- Rule from ig-agent §14.13 that still applies: never run git as root on the ig repos; fly-points
  has its own checkout and doesn't touch them.

If this ever feels like too much coupling, the same layout moves unchanged to a separate CX22
(about $5/mo).

## 5. Modifications before deployment

**Status (2026-11-24):** M1 and M3–M7 are implemented and committed locally, with the suite at 294
passed. M2 is a VM-side step with no code. The web app reads these settings from its environment;
all are unset locally, which keeps local behaviour unchanged. Put them in `/etc/flypoints/env` on
the VM:

```
FLYPOINTS_MAX_SEARCHES=8          # M3: per live search
FLYPOINTS_MAX_SWEEP_SEARCHES=40   # M3: per sweep
FLYPOINTS_DAILY_SEARCHES=60       # M3: live PointsYeah searches per day, all users together
FLYPOINTS_AUTH_MESSAGE=Award search is temporarily unavailable; ask Jean to refresh the search login.   # M4
```

Each item lists its verification. Run the suite after all of them:
`C:/Users/jeanv/.venvs/flypoints/Scripts/python.exe -m pytest -q`.

**M1. Secure and commit the working tree.** `.env`, which holds a `token=` value, is untracked and
not in `.gitignore`. Add `.env` to `.gitignore`, then commit the 23 modified and 12 new files. The
VM deploys from `origin/main`, and `jeanvelezdata/fly-points` is **public**, so nothing secret may
enter history.
*Verify:* `git status --ignored` lists `.env` as ignored; `git log -p origin/main | grep -i token=` is empty.

**M2. Python version parity.** The dev venv is 3.12; the VM's system Python is 3.11.2.
`requires-python >=3.11` allows both, but nothing has been tested on 3.11. Use uv-managed 3.12 on
the VM (`uv python install 3.12`) instead of system Python.
*Verify:* the test suite passes on the VM.

**M3. Bound what one request can spend.** `max_searches` is validated `>= 1` with no upper limit
(`web/app.py` `parse_params`), and sweeps default to 40. With a shared token, one form submission
can queue hundreds of PointsYeah searches. Add:
- a server-side cap on `max_searches`, for example 8 for a search and 40 for a sweep, configurable
  by env var;
- a rolling daily budget of live PointsYeah searches across all users, for example 60/day, kept in
  the existing `Cache` table. Past the budget, jobs fall back to stored results (`engine.search_stored`).
*Verify:* tests that `max_searches=500` returns a 422, and that a job beyond the budget runs stored-only.

**M4. Friend-facing auth error.** `AuthError` text tells the reader to copy a cookie out of
DevTools. That is right for Jean, wrong for friends. In the web layer only, show "Award search is
temporarily unavailable; ask Jean to refresh the search login" and log the full message server-side.
*Verify:* a test with a raising `search_fn` checks the rendered text.

**M5. Show operating flights in results.** Add a "Flights" column to the list view, e.g.
`IB382` or `AV259 · AV180 · IB424`. `views.segment_lines` already formats segments for the booking
panel. Also note under the existing `carriers` filter that it requires *every* segment to match, so
`IB` keeps the IB382 nonstop but drops the AV-connecting itineraries.
*Verify:* a test renders a LifeMiles offer with an IB segment and asserts that `IB382` appears;
also check the page visually against a stored SJU→MAD result.

**M6. Coverage notice.** On the search form, list the reachable programs that have no live source
(EI, BA, IB, SQ, CX, EY, AY, QR, TP, BR, JL). The simplest version is a `live: false` key per
program in `programs.yaml`, read by `config.load_programs`.
*Verify:* a config test checks the flag; check the form visually.

**M7. Cache growth.** `cache.sqlite` is already 71 MB locally after five weeks, and
`Cache.purge_expired()` is never called. Call it once at web-app startup.
*Verify:* a unit test seeds an expired row, creates the app, and asserts the row is gone.

Not needed: CORS or proxy-header changes (HTMX uses relative URLs, and the app builds no absolute
links), a database server (SQLite with one writer process is fine at this scale), or Docker (the VM
already runs native systemd services; adding Docker only for this app would add more risk than it
removes).

## 6. Deployment runbook (VM)

Run as root over the existing access (`ssh -i ~/.ssh/ig_agent_vm root@<VM_IP>`; the IP is in
`~/.ssh/known_hosts` and is deliberately not written here).

1. **User and dirs.**
   `useradd --system --home-dir /var/lib/flypoints --create-home --shell /usr/sbin/nologin flypoints`;
   `install -d -o flypoints -g flypoints /srv/flypoints`; `install -d -m 0750 -o root -g flypoints /etc/flypoints`.
2. **Code.** As `flypoints`: `git clone https://github.com/jeanvelezdata/fly-points.git /srv/flypoints`,
   install `uv`, then `uv python install 3.12`, `uv venv /srv/flypoints/.venv --python 3.12`, and
   `uv pip install --python /srv/flypoints/.venv/bin/python -e "/srv/flypoints[dev]"`.
   *Verify:* `pytest -q` passes in `/srv/flypoints`.
3. **Secrets.** Create `/etc/flypoints/env` (mode 0640, root:flypoints) containing
   `POINTSYEAH_REFRESH_TOKEN=…`, the four `FLYPOINTS_*` limits from §5, and, optionally, the
   SerpApi key. Type it in on the VM; never paste
   it into a tracked file or a chat.
4. **Service.** `/etc/systemd/system/flypoints.service`: `User=flypoints`,
   `EnvironmentFile=/etc/flypoints/env`, `WorkingDirectory=/srv/flypoints`,
   `ExecStart=/srv/flypoints/.venv/bin/flypoints web --host 127.0.0.1 --port 8765`, `Restart=on-failure`, plus the hardening from §4. Keep the unit file
   in the repo under `ops/` so it is versioned.
   *Verify:* `ss -ltn` shows `127.0.0.1:8765`, not `0.0.0.0`; `curl -s localhost:8765/` returns the form.
5. **Live smoke test from the VM.** Run one search, SJU→MAD, 4-day window, all cabins.
   *Verify:* PointsYeah returns results, so the token works from a datacenter IP, and at least one
   cash fare attaches. fast-flights scrapes Google Flights and is untested from Hetzner's IP range.
   If cash comes back empty, set the SerpApi key (250 free searches/month) or run with cash off.
6. **Hostname.** Either an A record on a domain Jean owns, or no-cost `<ip-with-dashes>.sslip.io`.
   Either works with Let's Encrypt; a real domain is easier to tell friends.
7. **Caddy.** Install from Caddy's official apt repository. `/etc/caddy/Caddyfile`:
   ```
   <host> {
       basic_auth {
           friends <bcrypt hash from `caddy hash-password`>
       }
       reverse_proxy 127.0.0.1:8765
   }
   ```
   Use a long random passphrase; Caddy has no built-in brute-force lockout.
8. **Firewall.** `ufw allow 80/tcp && ufw allow 443/tcp`, plus the Hetzner Cloud Firewall if one
   is attached.
   *Verify, from outside the VM:* `curl -I https://<host>/` returns 401; with the password it
   returns 200; `curl -I https://<VM_IP>/` fails the TLS handshake (no site for a bare IP); and
   `https://review.prismapr.org/` still shows Cloudflare Access, i.e. ig-agent is unchanged.

## 7. Operations

- **Update:** `sudo -u flypoints git -C /srv/flypoints pull --ff-only && systemctl restart flypoints`.
  A restart drops in-memory jobs; anyone mid-search sees "Unknown job" and resubmits.
- **Token expiry:** every search fails with M4's message. Copy a fresh `…refreshToken` cookie from
  pointsyeah.com, update `/etc/flypoints/env`, restart.
- **Data files:** `programs.yaml` ratios were verified 2026-10-19, so the 90-day stale warning
  starts on 2027-01-17. Edit `bonuses.yaml` and `programs.yaml` on the laptop, push, then pull on
  the VM. Never edit them on the VM, or the checkout diverges.
- **Logs:** `journalctl -u flypoints -u caddy`. Never log token contents; the provider already
  avoids that.
- **Backups:** only `saved_searches.json` is worth keeping. Caches and the offer store rebuild
  themselves.

## 8. Risks and open items

- **PointsYeah account risk.** Friends' traffic looks like one heavy user. PointsYeah could
  rate-limit or close the account; check its terms. M3's budget is the main mitigation.
- **Public listener on a production VM.** It is mitigated by §4, but it is still a change to
  PRISMA infrastructure posture that ig-agent's design avoided on purpose. Moving to a separate
  CX22 is the clean exit.
- **One shared password.** No way to revoke one friend. Rotating it means telling everyone.
- **Coverage.** Half the reachable programs, including Iberia, have no live source (§2). Friends
  should see that in the UI (M6), not discover it.
- **Cash fares depend on a consent cookie** (see the deviations at the top). The fallback, if it
  breaks, is a SerpApi key: `SERPAPI_API_KEY` in the env file, 250 free searches/month.
- **Resource fit.** The VM had about 3 GB available and 28 GB of free disk on 2026-11-24.
  fly-points' footprint on it hasn't been measured; check `systemctl status flypoints` memory after
  a sweep.
