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
