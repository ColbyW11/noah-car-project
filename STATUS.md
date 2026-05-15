# Project Status — 2026-05-13 (3 of 3 active dealers working)

## Where the data lives

Each daily run writes to two places:

- **Raw, per-day**: `data/raw/<YYYY-MM-DD>/`
  - `observations.jsonl` — one JSON line per dealer scrape (success or
    structured error), schema in [`SPEC.md`](SPEC.md). Atomic write +
    idempotent re-run replaces the date's partition.
  - `run_metadata.json` — run-level summary (start/end time, success and
    error counts, scraper version).
- **Processed, append-only**: `data/processed/timeseries.parquet`
  — every successful observation across all dates, flattened (no
  per-slot detail; that lives in the raw JSONL). Same-day re-runs
  replace that date's rows, never duplicate.

Both paths are local-only. Google Drive sync is plumbed
(`src/vw_scraper/storage/drive.py`, `scripts/sync_drive.py`) but the
service-account credentials in [`SETUP.md`](SETUP.md) §A have not been
configured yet, so syncing to Drive is a no-op until those are set up.

## How it runs

Two launchd jobs:

| When | Job | Plist | What it does |
| --- | --- | --- | --- |
| **Daily, 9 AM** | `com.colby.vw-scraper.daily` | `~/Library/LaunchAgents/com.colby.vw-scraper.daily.plist` | Runs `scripts/run_daily.py` → writes JSONL + parquet, regenerates analytics, fires a macOS notification on failure or degraded run. |
| **Sundays, 10 AM** | `com.colby.vw-scraper.weekly` | `~/Library/LaunchAgents/com.colby.vw-scraper.weekly.plist` | Runs `scripts/health_check.py --notify`. Catches silent walker breakage that the daily notification misses — fires a banner if any active dealer had zero successes in the past 7 days. |
| **Daily, 4 AM** | `com.colby.vw-scraper.logrotate` | `~/Library/LaunchAgents/com.colby.vw-scraper.logrotate.plist` | Runs `scripts/rotate_logs.py`. Rotates any `~/Library/Logs/vw-scraper*.log` file >5MB, keeps 4 generations. Runs between the daily and weekly jobs so it never races a live writer. |

Logs (rotated nightly at 4 AM, see `com.colby.vw-scraper.logrotate`):
- Daily: `~/Library/Logs/vw-scraper.{out,err}.log`
- Weekly: `~/Library/Logs/vw-scraper-weekly.{out,err}.log`
- Logrotate itself: `~/Library/Logs/vw-scraper-logrotate.{out,err}.log`

Common operations:

```bash
launchctl list | grep vw-scraper                                    # which jobs are loaded
launchctl print gui/$UID/com.colby.vw-scraper.daily                 # status + next fire time
launchctl kickstart -k gui/$UID/com.colby.vw-scraper.daily          # run the daily job NOW
launchctl bootout gui/$UID/com.colby.vw-scraper.daily               # stop scheduling
launchctl bootstrap gui/$UID/ ~/Library/LaunchAgents/<plist>        # (re)load a plist
```

If the laptop is asleep at 9 AM, launchd fires the job at next wake.
Network failures, dealer-side outages, etc. don't break the schedule —
the next day's run is independent.

## Running manually

```bash
uv run python scripts/run_daily.py            # all active dealers → data/raw + data/processed
uv run python scripts/scrape_one.py VW0002    # single dealer → stdout
uv run python scripts/run_daily.py --headed   # show the browser (debug only)
uv run python scripts/run_daily.py --skip-timeseries  # raw JSONL only
uv run python scripts/analyze.py              # weekly report → data/reports/
uv run python scripts/health_check.py         # per-dealer success summary
```

## Dealer status

| Code   | Dealer             | Status              | Slots/day | Notes                                                                                                                            |
| ------ | ------------------ | ------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------- |
| VW0001 | Teddy VW           | **inactive**        | —         | Dealer disabled online scheduling on their site (`enableScheduleServiceButtons = false`). Phone-only via (718) 920-1400.         |
| VW0002 | Jeff D'Ambrosio VW | ✅ **working**      | ~117      | consumer.xtime.com modern SPA, webKey `vw20120702001037406497`.                                                                  |
| VW0003 | Piazza VW          | **inactive**        | —         | Host unreachable (port 443 ECONNREFUSED). Redirect target `piazzaautogroup.com` is Akamai-bot-blocked. Possibly defunct dealer.  |
| VW0004 | VW of West Islip   | ✅ **working**      | ~197      | TeamVelocity inline form on dealer.com, `xtime.teamvelocityportal.com` backend.                                                  |
| VW0005 | VW of Nanuet       | ✅ **working**      | ~88       | ConnectCDK 5-step wizard via `api.connectcdk.com` iframe + transport-modal dialog. Slots from `/Availability/AvailableSlots`.    |

## Fixing the missing dealers

**VW0001 Teddy** — Nothing to scrape until the dealer re-enables online
scheduling. Their `/ScheduleService` page intentionally renders empty for
all visitors. If they ever turn it back on, the `secureoffersites.com`
Vue3 SPA will need its own scraper (it's neither Xtime nor ConnectCDK).
**Action**: monitor — re-check once a quarter by curling the page and
grepping for `enableScheduleServiceButtons = '...'`. No code change
needed today.

**VW0003 Piazza** — DNS resolves to `64.70.56.99` but TCP 443 refuses;
HTTP 80 redirects to `piazzaautogroup.com/locations/volkswagen.htm`
which returns 403 (Akamai BOT-BROWSER-IMPERSONATOR). Likely the dealer
has migrated or shut down. **Action**: verify whether the dealership is
still operating; if so, find their new schedule URL (possibly on the
parent `piazzaautogroup.com` group site).

**VW0005 Nanuet (ConnectCDK)** — Resolved. Walker drives the full
5-step wizard: NEW CUSTOMER → vehicle picker (Make/Year/Model/mileage
via React-aware native value setter) → catalog "Oil And Filter -
Change" with disclaimer modal confirm → page-NEXT opens transport
modal → pick "I will drop off my vehicle" radio →
`transportation-dialog-next-button` advances to time page → CDK fires
`/Availability/AvailableSlots?cid=2001816` with a list of `{date,
isAvailable}` slots. Parser now filters `isAvailable: false` and
accepts `date` as the timestamp key.

## Action items, ranked

1. **(yours, 30 min) Cut over to GitHub Actions for production.**
   Workflow is wired and waiting in
   [`.github/workflows/daily-scrape.yml`](./.github/workflows/daily-scrape.yml).
   See [`DEPLOY.md`](./DEPLOY.md) for full context. Steps:
   1. `gh repo create ColbyW11/vw-scraper-data --private`
      — empty private repo for the daily data drops.
   2. Generate a fine-grained PAT (Settings → Developer settings → PATs)
      scoped to the new repo, with **contents: read & write**.
   3. `gh secret set VW_SCRAPER_DATA_TOKEN` (paste the PAT).
   4. `gh secret set VW_SCRAPER_SLACK_WEBHOOK` (or skip — workflow runs
      without it; alerts just become no-ops).
   5. Push the current branch. In the Actions tab, run "Daily scrape"
      via `workflow_dispatch` to smoke-test.
   6. After 7 days of clean GH runs in parallel, bootout the laptop
      launchd jobs:
      `launchctl bootout gui/$UID/com.colby.vw-scraper.{daily,weekly,logrotate}`.
2. **(yours)** Verify VW0003 Piazza dealer status — phone the
   dealership at `(610) 896-4853`. If the dealership is operating from
   a new web address, update the registry.
3. **(yours, optional)** Set up Google Drive credentials per
   [`SETUP.md`](SETUP.md) if you also want Drive mirroring.
   `ci_run.py` now skips Drive when env vars are unset, so it's purely
   additive on top of the data-repo push.

### Done

- ✅ All three active dealers producing slot data (~402 slots/day total).
- ✅ Wired `append_to_timeseries` into `run_daily.py` — every run also
  updates `data/processed/timeseries.parquet`.
- ✅ Scheduled via launchd at 9 AM daily (plist in `~/Library/LaunchAgents/`).
- ✅ macOS notification on degraded or failed runs — silent on full
  success, banner with sound when `success_count < dealers_attempted`.
  Suppress with `--no-notify` for manual debugging.
- ✅ ConnectCDK walker fully wired: catalog + disclaimer modal +
  transport modal + slot XHR parser.
- ✅ Analytics tooling (`scripts/analyze.py`) — answers the three
  SPEC.md questions (network avg lead time, next-day rate, per-dealer
  friction) plus an availability heatmap. Writes 4 PNGs +
  `weekly_summary.md` to `data/reports/`.
- ✅ Health-check script (`scripts/health_check.py`) — per-dealer
  success rate over last N days, flags dealers with zero successes
  (catches walker breakage that the per-run notification misses).
- ✅ Generic dealer probe preserved at
  `scripts/diagnostics/probe_dealer_page.py` for future debugging or
  onboarding new dealers.
- ✅ `SETUP.md` trimmed — now only documents the optional Drive
  credentials; pipeline-state lives in this file.
- ✅ `analyze.py` runs at the end of every daily run, so
  `data/reports/` always reflects the latest observation (no manual
  step needed).
- ✅ Weekly health check scheduled — Sunday 10 AM via the
  `com.colby.vw-scraper.weekly` launchd plist. Fires a banner if any
  active dealer was silent the past 7 days.
- ✅ Log rotation wired — `scripts/rotate_logs.py` runs nightly at 4 AM
  via `com.colby.vw-scraper.logrotate`. Caps each log stream at
  ~20MB (5MB × 4 rotations).

## What's in good shape

- 100 unit tests pass, mypy `--strict` clean.
- Three-envelope Xtime parser covers consumer.xtime, TeamVelocity, and
  legacy variants — adding new dealers on those platforms is a CSV row
  append.
- ConnectCDK parser is solid (handles bare list, dict-with-slot-key,
  and ISO-string slot lists).
- Real-world bug fixed: `RobotsCache` was being 403'd by DealerOn's
  Varnish on its default `Python-urllib` UA, silently disallowing
  Jeff. Now fetches `robots.txt` with our identifying UA.
- Walker handles three Xtime UI variants (consumer.xtime SPA, Jeff's
  iframe-embedded variant, TeamVelocity inline) and dismisses West
  Islip's "Already a customer?" intercepting modal.
- Output is idempotent: same-day re-runs replace the partition,
  verified with `wc -l` on the JSONL across two runs.
