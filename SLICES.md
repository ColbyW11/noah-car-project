# Build Plan — Vertical Slices

Each slice is one Claude Code session. Do them in order. Do not start slice N+1 until slice N is committed and tests pass.

**Rule:** At the end of each slice, you should have something that works end-to-end within its scope, not a half-built layer.

---

## Slice 0 — Project scaffolding
**Goal:** Empty but runnable project.

Prompt to give Claude Code:
> Read SPEC.md and CLAUDE.md. Set up a Python 3.11 project using uv. Add dependencies: playwright, polars, pyarrow, structlog, pytest, pytest-asyncio, pydantic, google-api-python-client, google-auth. Create the directory structure in SPEC.md's storage layout section, but under the repo's `src/` and `tests/` directories (not the Drive layout). Create empty `__init__.py` files, a `pyproject.toml` with project metadata, a `.gitignore` that excludes data/ raw outputs but keeps tests/fixtures, a `README.md` with setup instructions, and a trivial smoke test (`test_smoke.py`) that imports the package. Run `uv sync`, then `uv run playwright install chromium`, then `uv run pytest`. Commit.

**Done when:** `uv run pytest` passes, `uv run python -c "import vw_scraper"` works.

---

## Slice 1 — Dealer registry loader
**Goal:** Load and validate the dealer registry from CSV.

Prompt:
> Build `src/vw_scraper/registry.py`. Define a `DealerConfig` pydantic model matching the dealer registry schema in SPEC.md. Write `load_registry(path: Path) -> list[DealerConfig]` that reads the CSV, validates each row, parses `config_json`, and returns active dealers only. Write `tests/test_registry.py` with fixtures: a valid CSV, a CSV with an invalid platform value, a CSV with malformed `config_json`, a CSV with a mix of active/inactive rows. Also commit a starter `data/dealer_master.csv` with 5 placeholder rows (use `VW0001` through `VW0005`, platform `unknown`, blank URLs — we'll fill these in during Slice 2). Tests must pass.

**Done when:** `uv run pytest tests/test_registry.py` passes and `load_registry` returns 5 dealers from the starter CSV.

---

## Slice 2 — Platform discovery
**Goal:** Identify which scheduling platform each of the 5 dealers uses.

Prompt:
> I have 5 VW dealer URLs (I will paste them in). For each dealer: launch Playwright, navigate to the site, find the service scheduling page, identify the platform (Xtime / myKaarma / Dealer-FX / other) by looking for known script URLs, iframe srcs, or DOM signatures. Save a full-page HTML snapshot and a screenshot to `tests/fixtures/discovery/<dealer_code>/`. Update `data/dealer_master.csv` with the platform, `schedule_url`, and any config_json hints you discovered (e.g., ZIP required, vehicle selection required). Write `src/vw_scraper/platform_detect.py` with a reusable `detect_platform(page) -> PlatformName` function. Commit the fixtures and the updated CSV.

**Done when:** The registry CSV has `platform` filled in for all 5 dealers and fixtures exist for each.

*Before running this slice, you'll paste in the 5 dealer URLs.*

---

## Slice 3 — First platform scraper (fixture-based)
**Goal:** A working parser for the most common platform from Slice 2, tested entirely against fixtures.

Prompt:
> The most common platform from Slice 2 is [PLATFORM]. Build `src/vw_scraper/scrapers/[platform].py` implementing the PlatformScraper protocol defined in SPEC.md. The scraper parses available oil change slots from a rendered page. Write it against the fixture HTML first — do not touch a live site in this slice. Define the `ScrapeResult` dataclass in `src/vw_scraper/models.py`. Define the `PlatformScraper` protocol in `src/vw_scraper/scrapers/base.py`. Write comprehensive parser tests in `tests/scrapers/test_[platform].py` covering: slots available, no slots available, malformed HTML, page with login wall. Use the fixtures from Slice 2.

**Done when:** Parser tests pass against all fixture scenarios; scraper returns well-typed `ScrapeResult` objects.

---

## Slice 4 — Live scrape for one dealer
**Goal:** End-to-end scrape of one real dealer, with flow timing.

Prompt:
> Wire up `src/vw_scraper/scrapers/[platform].py` to run against a live dealer. Add the navigation/form-fill logic (may require ZIP and vehicle selection — check the dealer's config_json). Instrument `scheduling_flow_seconds` (time from page load to slots visible) and `interaction_steps`. Add a `@pytest.mark.live` test that runs against dealer VW0001 and asserts we get a successful ScrapeResult with at least one slot or status='error' with a clear reason. Add a CLI entry point `scripts/scrape_one.py <dealer_code>` that runs a single dealer scrape and prints the JSON result.

**Done when:** `uv run python scripts/scrape_one.py VW0001` prints a valid ScrapeResult.

---

## Slice 5 — Orchestration and raw storage
**Goal:** Run all 5 dealers concurrently, write raw JSONL.

Prompt:
> Build `src/vw_scraper/orchestrator.py` with `run_daily(registry_path, output_dir, concurrency=5)`. It loads the registry, launches one shared browser, dispatches dealers concurrently using `asyncio.Semaphore`, collects ScrapeResults, and writes one JSONL line per dealer to `<output_dir>/YYYY-MM-DD/observations.jsonl`. Also write `run_metadata.json` with start/end time, success/error counts, scraper version. Per-dealer timeout: 60s. One dealer's failure must not affect others. Add a CLI entry point `scripts/run_daily.py`. Write `tests/test_orchestrator.py` with a mock scraper that simulates successes and failures.

**Done when:** `uv run python scripts/run_daily.py` produces a valid daily JSONL file with 5 lines.

---

## Slice 6 — Processed time series
**Goal:** Append daily observations to a master Parquet file.

Prompt:
> Build `src/vw_scraper/storage/timeseries.py`. Function `append_to_timeseries(jsonl_path, parquet_path)` reads the day's observations, flattens them (drops `available_slots`, adds `observation_date`, `day_of_week`, `hour_of_day`), and appends to `timeseries.parquet` using polars. It must be idempotent: re-running the same day replaces that day's rows, not duplicates them. Also build `compute_daily_summary(parquet_path, date)` returning a dataframe with the three core metrics (network avg lead time, next-day rate, average scheduling flow seconds) for the given date. Tests with synthetic data covering: first write, append, re-run same day (idempotency), empty day.

**Done when:** Running the pipeline twice for the same day yields the same row count in `timeseries.parquet`.

---

## Slice 7 — Google Drive sync
**Goal:** Push raw + processed outputs to the Drive folder.

Prompt:
> Build `src/vw_scraper/storage/drive.py` using the Google Drive API v3 with a service account. Functions: `upload_file(local_path, drive_folder_id, remote_name)`, `download_file(drive_file_id, local_path)`, `sync_outputs(local_data_dir, drive_root_folder_id)`. The sync function mirrors the local `data/` directory into the Drive folder, creating subfolders as needed, and replacing files with newer local versions. Drive folder ID comes from env var `VW_SCRAPER_DRIVE_FOLDER_ID`. Service account JSON path from `VW_SCRAPER_SA_PATH`. Add retry with exponential backoff on Drive API errors. Tests with a mocked Drive client.

**Done when:** After a daily run, all new files appear in the target Drive folder.

---

## Slice 8 — Second platform scraper
**Goal:** Prove the platform-router pattern by adding a second scraper.

Prompt:
> The second platform from Slice 2 is [PLATFORM_2]. Build `src/vw_scraper/scrapers/[platform_2].py` following the same pattern as Slice 3 and 4. Build the platform router in `src/vw_scraper/scrapers/__init__.py`: `get_scraper(platform_name) -> PlatformScraper`. Update the orchestrator to use the router so each dealer is dispatched to the right scraper based on its registry entry. Add fixtures and fixture-tests for the new platform. Confirm the daily run still works end-to-end with mixed platforms.

**Done when:** Daily run successfully scrapes dealers across both platforms and writes a unified JSONL.

---

## Slice 9 — Scheduling and alerting
**Goal:** Run unattended daily with monitoring.

Prompt:
> Create `.github/workflows/daily-scrape.yml` — a GitHub Actions workflow that runs daily at a configurable cron time, installs the project, runs the scrape, and syncs to Drive. Secrets come from GitHub Actions secrets. On failure (any run-level exception) or degraded runs (>25% dealer failures), send a Slack webhook notification. Add `src/vw_scraper/alerts.py` with `send_slack_alert(message, severity)`. Document the required secrets in README.md.

**Done when:** A manual workflow dispatch produces a successful run and uploads to Drive.

---

## Slice 10 — Analytics notebook
**Goal:** Answer the three questions from SPEC.md.

Prompt:
> Create `notebooks/analysis.ipynb`. Load `timeseries.parquet` (download from Drive if not local). Produce: (1) a line chart of network average lead time over time, (2) a bar chart of next-day appointment rate by week, (3) a dealer-ranked table by `scheduling_flow_seconds` (friction ranking for the product use case), (4) a heatmap of availability by day-of-week and hour-of-day. Use polars for data, matplotlib for charts. Save static PNGs to `data/reports/` and a summary markdown to `data/reports/weekly_summary.md`.

**Done when:** The notebook runs top-to-bottom without error on real scraped data.

---

## After Slice 10

Scaling to 100+ dealers:
1. Add dealers to `dealer_master.csv` in batches of ~20, running Slice 2 (platform discovery) on each batch.
2. If a new platform appears that isn't Xtime / myKaarma / Dealer-FX, add a new scraper (repeats pattern from Slice 3/4).
3. When GitHub Actions runtime exceeds ~10 minutes, migrate to Cloud Run Jobs.
4. When the Parquet file exceeds ~1M rows, partition by month and load into DuckDB or BigQuery for analytics.

## Session Hygiene

At the end of every session, Claude Code should:
1. Run the full test suite. Report pass/fail.
2. Run `mypy src/`. Report any new errors.
3. Summarize what changed in 3–5 bullets.
4. List any deviations from SPEC.md or CLAUDE.md and flag them for review.
5. Suggest the next slice only after confirming the current one is complete.
