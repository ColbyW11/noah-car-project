# Project Status — 2026-06-26 (GitHub Actions is production)

## TL;DR

- **Production runs in the cloud.** GitHub Actions (`.github/workflows/daily-scrape.yml`)
  scrapes every active dealer daily at 13:00 UTC on a fresh Ubuntu runner,
  builds the processed time-series, and pushes `raw/<date>/` +
  `processed/timeseries.parquet` to the private `ColbyW11/vw-scraper-data`
  repo. $0/mo. Green daily since mid-June.
- **The laptop launchd jobs are retired.** The cloud is the single source of
  truth. (Plists remain on disk; `launchctl bootstrap` re-loads them if ever
  needed — see [Local mode](#local-mode-retired).)
- **3 of 3 active dealers working** again as of 2026-06-26. VW0002 Jeff was
  silently broken ~May 26 → June 26 by an Xtime transport-step markup change;
  fixed and re-producing ~150 slots/day.
- **Alerts reach a human via GitHub issues.** A degraded run (>25% dealer
  failures), an all-failed run, or an infra failure opens/updates a single
  rolling `scraper-health` issue (built-in `GITHUB_TOKEN`, no external secret).
  A healthy run closes it. Slack is still supported but optional/unset.

## Where the data lives

The daily cloud run writes to the `vw-scraper-data` repo:

- **Raw, per-day**: `raw/<YYYY-MM-DD>/`
  - `observations.jsonl` — one JSON line per dealer scrape (success or
    structured error), schema in [`SPEC.md`](SPEC.md). Atomic write +
    idempotent re-run replaces the date's partition.
  - `run_metadata.json` — run-level summary (start/end, success/error counts,
    scraper version).
- **Processed, append-only**: `processed/timeseries.parquet`
  — every successful observation across all dates, flattened. Same-day re-runs
  replace that date's rows, never duplicate. The cloud seeds each runner with
  the existing parquet before the run so it accumulates day over day. Backfilled
  May 17 → present from the retained raw partitions
  (`scripts/backfill_timeseries.py`).

For local analysis, `git clone`/`git pull` the data repo and point
`scripts/dashboard.py` / `scripts/analyze.py` at its
`processed/timeseries.parquet`.

Google Drive sync is still plumbed (`src/vw_scraper/storage/drive.py`) but
opt-in and unconfigured — `ci_run.py` skips it silently when the env vars are
unset.

## How it runs (production)

`.github/workflows/daily-scrape.yml`, on `schedule: "0 13 * * *"` (and
`workflow_dispatch`):

1. **Seed** — clone the data repo, copy its `processed/timeseries.parquet`
   into `./data/processed/` so the append accumulates.
2. **Pipeline** — `uv run python scripts/ci_run.py --alert-file …`: scrape all
   active dealers → append to the parquet → degraded/all-failed threshold check
   (writes the alert file on trouble).
3. **Push** — rsync `raw/` + copy the parquet into the data-repo clone, commit,
   push.
4. **Alert** — turn the alert file (or any infra failure) into a rolling
   `scraper-health` GitHub issue; close it on a healthy run.

Required secret: `VW_SCRAPER_DATA_TOKEN` (fine-grained PAT, contents:write on
`vw-scraper-data`). Optional: `VW_SCRAPER_SLACK_WEBHOOK`.

```bash
gh run list  --repo ColbyW11/noah-car-project --workflow "Daily scrape"   # recent runs
gh workflow run "Daily scrape" --repo ColbyW11/noah-car-project            # run now
gh issue list --repo ColbyW11/noah-car-project --label scraper-health      # open alerts
```

## Running manually (local)

```bash
uv run python scripts/run_daily.py            # all active dealers → data/raw + data/processed
uv run python scripts/scrape_one.py VW0002    # single dealer → stdout
uv run python scripts/run_daily.py --headed   # show the browser (debug only)
uv run python scripts/ci_run.py               # the exact cloud pipeline, locally
uv run python scripts/backfill_timeseries.py --raw-dir <data-repo>/raw \
    --timeseries-path <data-repo>/processed/timeseries.parquet  # rebuild parquet
uv run python scripts/analyze.py              # weekly report → data/reports/
uv run python scripts/health_check.py         # per-dealer success summary
uv run streamlit run scripts/dashboard.py     # interactive dashboard → http://localhost:8501
```

## Dealer status

| Code   | Dealer             | Status              | Slots/day | Notes                                                                                                                            |
| ------ | ------------------ | ------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------- |
| VW0001 | Teddy VW           | **inactive**        | —         | Dealer disabled online scheduling (`enableScheduleServiceButtons = false`). Phone-only via (718) 920-1400.                       |
| VW0002 | Jeff D'Ambrosio VW | ✅ **working**      | ~150      | consumer.xtime.com SPA, webKey `vw20120702001037406497`. Transport step is now a `.panel-drop-off__slot` checkbox row (see below). |
| VW0003 | Piazza VW          | **inactive**        | —         | Host unreachable (443 ECONNREFUSED); redirect target Akamai-bot-blocked. Likely defunct — phone (610) 896-4853 to confirm.       |
| VW0004 | VW of West Islip   | ✅ **working**      | ~150–200  | TeamVelocity inline form on dealer.com, `xtime.teamvelocityportal.com` backend.                                                  |
| VW0005 | VW of Nanuet       | ✅ **working**      | ~80–90    | ConnectCDK 5-step wizard via `api.connectcdk.com` iframe + transport-modal dialog. Slots from `/Availability/AvailableSlots`.    |

### VW0002 Jeff — the 2026-06 regression (fixed)

consumer.xtime's transportation step changed from `role="radio"` options to a
`<div class="panel-drop-off__slot">` row wrapping a
`<div role="checkbox" aria-label="I'll wait at the dealership"|"I have a ride">`.
The walker only looked for `[role='radio']`, selected nothing, clicked NEXT on
an invalid form, and the availability XHR never fired → a silent 120s timeout
every day. Fix: `TRANSPORT_OPTION_SELECTORS` in
`src/vw_scraper/scrapers/xtime.py` (click the `.panel-drop-off__slot` row, with
legacy radio + TeamVelocity `<label>` fallbacks). Regression fixture +
browser-marked test in `tests/scrapers/test_xtime_transport_step.py` /
`tests/fixtures/scrapers/xtime/transport_step_2026/`.

### Fixing the missing dealers

**VW0001 Teddy** — nothing to scrape until the dealer re-enables online
scheduling. Re-check quarterly: curl `/ScheduleService` and grep for
`enableScheduleServiceButtons = '...'`.

**VW0003 Piazza** — likely migrated or shut down. Verify the dealership is
operating; if so, find its new schedule URL and update the registry.

## Local mode (retired)

The laptop launchd jobs (`com.colby.vw-scraper.{daily,weekly,logrotate}`) have
been retired (`launchctl bootout`). The cloud is authoritative. The plists are
still on disk in `~/Library/LaunchAgents/`; to re-enable local runs:

```bash
launchctl bootstrap gui/$UID/ ~/Library/LaunchAgents/com.colby.vw-scraper.daily.plist
launchctl bootstrap gui/$UID/ ~/Library/LaunchAgents/com.colby.vw-scraper.weekly.plist
launchctl bootstrap gui/$UID/ ~/Library/LaunchAgents/com.colby.vw-scraper.logrotate.plist
```

## Action items, ranked

1. **(monitor)** Watch the `scraper-health` GitHub issue. It now surfaces any
   dealer regression within a day instead of silently degrading for weeks.
2. **(yours)** Verify VW0003 Piazza dealer status — phone (610) 896-4853. If it
   operates from a new web address, update the registry.
3. **(yours, optional)** Set up Google Drive credentials per [`SETUP.md`](SETUP.md)
   if you also want Drive mirroring (purely additive on top of the data-repo push).

## What's in good shape

- ~120 unit tests pass, `mypy --strict` clean.
- Three-envelope Xtime parser (consumer.xtime, TeamVelocity, legacy) — adding
  dealers on those platforms is a CSV row append.
- ConnectCDK parser handles bare list, dict-with-slot-key, and ISO-string lists.
- Output is idempotent: same-day re-runs replace the partition / the date's
  parquet rows, never duplicate.
- Failure isolation: one broken dealer never kills the run (per-dealer 120s cap
  + exception isolation).
