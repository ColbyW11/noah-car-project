# VW Oil Change Availability Tracker — Specification

## Purpose

Scrape VW dealer scheduling systems daily to record oil change availability. Build a time series that answers:

1. **Average across the network** — mean lead time to first available oil change, across all dealers, per day.
2. **Next-day appointment rate** — fraction of dealers with a first-available slot within 24–48 hours of observation.
3. **Length of scheduling** — two components:
   - `lead_time_hours`: time from observation to the first available slot.
   - `scheduling_flow_seconds`: wall-clock time from landing on the scheduling page to seeing available slots (measures user-facing friction).

Secondary goal: enable a "find me the fastest oil change near me" product lookup.

## Scope

- **Pilot**: 5 VW dealers.
- **Target**: 100+ VW dealers.
- **Every component must be designed to work identically at both scales.** No code changes when adding dealer #6 or #106 — only a row append to the registry.

## Core Architectural Principles

1. **Dealer registry is data, not code.** A CSV/JSON file drives everything. Adding a dealer is a row append.
2. **One scraper per platform, not per dealer.** A platform router dispatches to the right scraper based on the registry.
3. **Failure isolation.** One broken dealer cannot kill the daily run. Each dealer scrape is wrapped, errors are recorded, the loop continues.
4. **Parallel execution from day one.** Use `asyncio` with Playwright. Concurrency is a config knob.
5. **Idempotent daily writes.** Re-running a date replaces that date's partition cleanly.
6. **Loud failure, never silent drift.** A scraper that can't find slots returns `status='error'`, never an empty list that looks like "no availability."
7. **Raw data is sacred.** Always persist the full slot list, not just aggregates. Aggregates are recomputable; lost raw data is not.

## Data Model

### Dealer Registry (`data/dealer_master.csv`)

| column | type | notes |
|---|---|---|
| `dealer_code` | str | Primary key. Use VW's dealer code. |
| `dealer_name` | str | Display name. |
| `dealer_url` | str | Root URL of dealer site. |
| `schedule_url` | str | Direct URL to the scheduling page (skip navigation where possible). |
| `platform` | enum | `xtime` \| `mykaarma` \| `dealer_fx` \| `custom` \| `unknown` |
| `zip` | str | Dealer ZIP (sometimes required by scheduling widgets). |
| `region` | str | For geographic aggregations. |
| `config_json` | str | JSON blob with dealer-specific overrides (vehicle to select, any quirks). |
| `active` | bool | Skip inactive rows without deleting them. |
| `notes` | str | Free text. |

### Observation Record (one per dealer per scrape)

Written as one JSON line to `data/raw/YYYY-MM-DD/observations.jsonl`.

```json
{
  "dealer_code": "VW1234",
  "observation_ts": "2026-04-19T14:03:22Z",
  "scrape_status": "success",
  "error_message": null,
  "first_available_ts": "2026-04-20T09:00:00-07:00",
  "lead_time_hours": 18.93,
  "available_slots": [
    "2026-04-20T09:00:00-07:00",
    "2026-04-20T10:30:00-07:00",
    "2026-04-21T08:00:00-07:00"
  ],
  "slot_count": 3,
  "scheduling_flow_seconds": 12.4,
  "interaction_steps": 3,
  "platform": "xtime",
  "source_payload_hash": "sha256:abc123...",
  "scraper_version": "0.3.1"
}
```

### Processed Time Series (`data/processed/timeseries.parquet`)

Flattened, append-only. One row per successful observation. Columns match the observation record minus `available_slots` (kept in raw) plus derived fields (`observation_date`, `day_of_week`, `hour_of_day`).

## Storage Layout (Google Drive)

```
/vw-oil-availability/
  /dealers/
    dealer_master.csv              # source of truth for the registry
  /raw/
    2026-04-19/
      observations.jsonl           # one line per dealer
      run_metadata.json            # run start/end, success counts, version
  /processed/
    timeseries.parquet             # master append-only table
    daily_summary.parquet          # one row per dealer per day
  /reports/
    network_daily.csv              # rolling aggregates, updated daily
  /fixtures/
    # HTML snapshots for regression testing, organized by dealer_code/date
```

Drive is the persistence layer. For analytics at scale, load `timeseries.parquet` into DuckDB or BigQuery — don't query Drive directly.

## Tech Stack

- **Language**: Python 3.11+
- **Dependency manager**: `uv`
- **Browser automation**: Playwright (async API)
- **Data**: `polars` for dataframes, `pyarrow` for Parquet
- **Testing**: `pytest` with `pytest-asyncio`
- **Drive integration**: `google-api-python-client` with a service account
- **Scheduling**: cron locally → GitHub Actions scheduled workflows at scale
- **Logging**: `structlog` with JSON output

## Platform Scraper Contract

Every platform scraper implements:

```python
class PlatformScraper(Protocol):
    platform_name: str

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult:
        """Never raises. Returns ScrapeResult with status='success' or 'error'."""
```

Where `ScrapeResult` is a dataclass matching the observation record schema.

## Metric Definitions

- `lead_time_hours = (first_available_ts - observation_ts).total_seconds() / 3600`
- `next_day_rate_pct = 100 * count(lead_time_hours <= 48) / count(total_successful)`
- `network_avg_lead_time = mean(lead_time_hours)` over successful observations for the window
- `scheduling_flow_seconds = t_slots_visible - t_page_load_start` instrumented inside the scraper

## Failure Handling

- Per-dealer timeout: 60 seconds hard cap.
- Per-dealer retry: 1 retry on network errors, 0 retries on parse errors (parse errors indicate site change, retrying won't help).
- Run-level failure: if >25% of dealers fail on a given day, send an alert but still write what succeeded.
- Silent zero-slot guard: if a dealer returns zero slots for 3 consecutive days, flag for manual review.

## Legal & Operational Constraints

- Respect `robots.txt`.
- Honest `User-Agent` identifying the scraper and a contact email.
- Rate limit: no more than 1 concurrent request per dealer domain.
- Never book appointments. Read-only.
- Do not submit personal information in any form fields. Use dummy data only where required to reach availability (and record which dealers require this — it's product signal).

## Non-Goals

- Not scraping non-VW dealers (for now).
- Not scraping service types other than oil change (for now).
- Not predicting future availability — only recording observed availability.
- Not building a user-facing product yet — this is the data layer.

## Open Questions

- Exact VW dealer code format — confirm with first 5 dealers.
- Whether any dealers use a VW-branded first-party scheduler vs. third-party widgets only.
- Whether Xtime exposes a JSON endpoint we can hit directly (inspect network traffic during platform discovery).
