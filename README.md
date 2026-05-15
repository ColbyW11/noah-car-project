# VW Oil Change Availability Tracker

Scrapes VW dealer scheduling systems daily to build a time series of
oil-change availability. The pilot tracks 3 dealers in the NY/PA
metro area (Jeff D'Ambrosio, VW of West Islip, VW of Nanuet) and
captures ~400 timeslots per daily run.

**Start here for current state**: [`STATUS.md`](./STATUS.md) — which
dealers are working, what's pending, how the scheduled job is wired,
how to run anything manually.

Other reference docs:
- [`SPEC.md`](./SPEC.md) — authoritative project definition + data model
- [`CLAUDE.md`](./CLAUDE.md) — coding conventions
- [`DEPLOY.md`](./DEPLOY.md) — production deployment plan (3 paths
  with tradeoffs; recommended path for a small startup)
- [`SLICES.md`](./SLICES.md) — historical build plan (slices 0–10, all
  completed)
- [`SETUP.md`](./SETUP.md) — _optional_ Google Drive / GitHub Actions
  setup (not required for the local pipeline)

## Quick start

```bash
# Prereqs: Python 3.11+, uv (https://github.com/astral-sh/uv),
# Google Chrome installed (Playwright uses it via channel="chrome")

uv sync
uv run playwright install chromium firefox webkit  # webkit/firefox optional
uv run pytest                                      # confirm tests pass

# One-off: scrape every active dealer and write data/raw + parquet + report.
uv run python scripts/run_daily.py

# Single dealer to stdout (useful for debugging).
uv run python scripts/scrape_one.py VW0002

# Generate the static weekly report on demand.
uv run python scripts/analyze.py

# Per-dealer success rate over the last 7 days.
uv run python scripts/health_check.py

# Interactive dashboard (Streamlit) — opens at http://localhost:8501
uv run streamlit run scripts/dashboard.py
```

## Scheduled runs (macOS launchd)

Two jobs run on your laptop unattended. Full operational details live
in [`STATUS.md`](./STATUS.md#how-it-runs).

| When | Job | What it does |
| --- | --- | --- |
| Daily 9 AM | `com.colby.vw-scraper.daily` | Scrape → JSONL + parquet → regenerate report. macOS notification on failure. |
| Sunday 10 AM | `com.colby.vw-scraper.weekly` | Health check — banner if any active dealer was silent the past 7 days. |

Plists are in `~/Library/LaunchAgents/`; logs in `~/Library/Logs/`.

## Interactive dashboard

`uv run streamlit run scripts/dashboard.py` opens a browser-based
dashboard at `http://localhost:8501` with three tabs:

- **Availability** — lead-time trend per dealer, day×hour slot heatmap,
  slots-offered-per-future-date bars.
- **Scraper health** — per-dealer × per-day status grid, error-category
  breakdown, scheduling-flow box plots, run duration.
- **Per-dealer comparison** — ranked dealer table joined with the
  registry, friction-vs-availability scatter.

Sidebar filters (date range, dealer subset, include-errors toggle)
apply to every chart. Data is read from `data/processed/timeseries.parquet`
and the raw JSONL — no server, no extra setup. The cache is keyed on
file mtime, so re-running `scripts/run_daily.py` and refreshing the
browser is enough to see new data.

## What lands each run

```
data/
  raw/YYYY-MM-DD/
    observations.jsonl       # one line per dealer (success or error)
    run_metadata.json        # run summary (counts, version, duration)
  processed/
    timeseries.parquet       # append-only successful observations
  reports/                   # regenerated after each daily run
    weekly_summary.md
    lead_time_trend.png
    next_day_rate.png
    dealer_friction.png
    availability_heatmap.png
```

## Project structure

```
src/vw_scraper/
  registry.py           # Load dealer_master.csv
  platform_detect.py    # Identify scheduling platform
  http.py               # Identifying UA + RobotsCache
  models.py             # ScrapeResult, DealerConfig, etc.
  orchestrator.py       # Daily run loop (concurrency, atomic writes)
  scrapers/
    base.py             # PlatformScraper protocol
    xtime.py            # Xtime scraper (consumer.xtime + TeamVelocity variants)
    connect_cdk.py      # ConnectCDK scraper (api.connectcdk.com)
  storage/
    timeseries.py       # Parquet append (idempotent on observation_date)
    drive.py            # Google Drive sync (optional — see SETUP.md)
  alerts.py             # Slack alerts (optional)
scripts/
  run_daily.py          # CLI: full daily run + parquet + report
  scrape_one.py         # CLI: single dealer scrape
  analyze.py            # CLI: regenerate weekly report
  dashboard.py          # Streamlit: interactive 3-tab dashboard
  health_check.py       # CLI: per-dealer success rate
  discover_platforms.py # CLI: identify platform for new dealers
  diagnostics/          # Probes for debugging walker breakage
data/                   # Local outputs (git-ignored)
tests/
  fixtures/             # HTML snapshots for parser regression tests
```

## The three metrics

From the analytics report (`data/reports/weekly_summary.md`):

1. **Network average lead time** — mean hours from observation to first
   available oil-change slot.
2. **Next-day appointment rate** — % of dealers with first slot ≤ 48
   hours of observation.
3. **Scheduling flow seconds** — wall-clock time from page-load to slot
   list visible. Per-dealer friction ranking for the eventual
   "fastest oil change near me" product.

## Operational principles

- **Loud failures, never silent drift.** A scraper that can't find
  slots errors out with a structured prefix (`TIMEOUT:` / `PARSE:` /
  `NAVIGATION:` / `UNEXPECTED:`); it never returns an empty list.
- **Raw data is sacred.** Full slot lists are persisted in
  `observations.jsonl`. The parquet is a flattened derived view;
  aggregates are recomputable from the raw layer.
- **Failure isolation.** One broken dealer never kills the run —
  per-dealer timeouts and exception isolation keep the rest going.
- **Read-only.** The walker submits dummy data only where required to
  reach availability (per SPEC.md §144). It never books an appointment
  and never submits real personal information.
- **Identifiable but Mozilla-compatible UA.** Pure product-token UAs
  triggered Xtime to serve a degraded SPA bundle that never finished
  rendering. The UA now follows the Bingbot/Yandexbot pattern
  (`Mozilla/... (compatible; vw-oil-availability-scraper; +mailto:...)`)
  so dealers can still identify the scraper in their access logs.
