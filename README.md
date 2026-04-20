# VW Oil Change Availability Tracker

Scrapes VW dealer scheduling systems daily to build a time series of oil change availability.

See [`SPEC.md`](./SPEC.md) for the authoritative project definition, [`CLAUDE.md`](./CLAUDE.md) for coding conventions, and [`SLICES.md`](./SLICES.md) for the build plan.

## Quick start

```bash
# Prereqs: Python 3.11+, uv (https://github.com/astral-sh/uv)

# Install dependencies
uv sync
uv run playwright install chromium

# Configure (copy and edit)
cp .env.example .env

# Run tests
uv run pytest

# Scrape a single dealer (after Slice 4)
uv run python scripts/scrape_one.py VW0001

# Run the full daily pipeline (after Slice 5)
uv run python scripts/run_daily.py
```

## Required secrets

Stored outside the repo:

- `VW_SCRAPER_SA_PATH` — path to Google Drive service account JSON (e.g. `~/.config/vw-scraper/service_account.json`).
- `VW_SCRAPER_DRIVE_FOLDER_ID` — the ID of the root Drive folder for outputs.
- `VW_SCRAPER_SLACK_WEBHOOK` — optional, for failure alerts.

## Scheduled runs (GitHub Actions)

The `daily-scrape` workflow (`.github/workflows/daily-scrape.yml`) runs the
full pipeline — scrape, Drive sync, alert — daily at **13:00 UTC (≈ 9am ET)**
and on manual dispatch.

### Required repo secrets

Set these in *Settings → Secrets and variables → Actions*:

- `GCP_SERVICE_ACCOUNT_JSON` — the full JSON body of the Drive service account
  key (paste it in as-is, including newlines).
- `VW_SCRAPER_DRIVE_FOLDER_ID` — same ID used locally.
- `VW_SCRAPER_SLACK_WEBHOOK` — incoming-webhook URL for a Slack channel. If
  absent, the pipeline runs fine but no alerts are sent.

### Manual dispatch

```bash
gh workflow run daily-scrape.yml
gh run watch
```

Or use the *Actions* tab → *Daily scrape* → *Run workflow*.

### Alert thresholds

- **100% dealer failure** → Slack `:rotating_light:` (error), workflow exit 2.
- **>25% dealer failure** → Slack `:warning:` (warning), workflow exit 0.
- **Run-level exception** (scrape or Drive sync raises) → Slack error.
- **Infrastructure failure** (checkout / setup / Playwright install) → Slack
  error from a trailing `if: failure()` step.

## Drive folder layout

See the Storage Layout section of [`SPEC.md`](./SPEC.md).

## Project structure

```
src/vw_scraper/
  registry.py           # Load dealer_master.csv
  platform_detect.py    # Identify scheduling platform
  models.py             # ScrapeResult, DealerConfig, etc.
  orchestrator.py       # Daily run loop
  scrapers/
    base.py             # PlatformScraper protocol
    xtime.py            # Xtime scraper
    connect_cdk.py      # ConnectCDK scraper
    mykaarma.py         # myKaarma scraper (later)
  storage/
    timeseries.py       # Parquet append logic
    drive.py            # Google Drive sync
  alerts.py             # Slack notifications
tests/
  fixtures/             # HTML snapshots for regression tests
scripts/
  scrape_one.py         # CLI: scrape one dealer
  run_daily.py          # CLI: full daily run
  ci_run.py             # CLI: scrape + Drive sync + alerts (used by CI)
  sync_drive.py         # CLI: standalone Drive sync
notebooks/
  analysis.ipynb        # Answers the three core metrics
data/                   # Local outputs — not committed
  raw/YYYY-MM-DD/observations.jsonl
  processed/timeseries.parquet
```

## The three metrics

1. **Network average lead time** — mean hours from observation to first available oil change slot.
2. **Next-day appointment rate** — % of dealers with first slot within 48 hours of observation.
3. **Scheduling flow seconds** — wall-clock time from landing on the scheduling page to seeing slots. Measures user-facing friction.

## Operational principles

- **Loud failures, never silent drift.** A scraper that can't find slots errors out; it does not return an empty list.
- **Raw data is sacred.** Full slot lists are always persisted. Aggregates are recomputable.
- **Failure isolation.** One broken dealer never kills the run.
- **Read-only.** Never book an appointment. Never submit personal information.
