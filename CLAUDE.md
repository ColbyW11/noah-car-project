# CLAUDE.md — Project Conventions

**Read `SPEC.md` before any substantive work.** It is the authoritative definition of what this project does and why. This file is about *how* we build it.

## Workflow Rules

- Work in **vertical slices**. A slice is a thin end-to-end feature that can be tested and committed. Prefer "one dealer, one platform, end-to-end" over "all scrapers, no storage."
- **Write tests alongside code**, not after. Every parser gets a fixture-based test before it gets a live-site test.
- **Commit on every green state.** Small commits with clear messages.
- **Ask before large refactors.** If a task implies touching more than 3 files not named in the task, stop and confirm.
- **Never silently catch exceptions.** If you catch, log the traceback and record the error in the `ScrapeResult`. No bare `except:` clauses.

## Code Conventions

### Python
- Python 3.11+. Use modern syntax: `match` statements, PEP 604 unions (`str | None`), `Self` type.
- Type hints everywhere. `mypy --strict` should pass on `src/`.
- Dataclasses or Pydantic models for structured data. No dicts as records in public APIs.
- Async by default for any I/O. Use `asyncio`, not threads.
- `pathlib.Path` for filesystem, never string paths.

### Playwright
- Use the **async API**: `from playwright.async_api import ...`. Never mix sync and async.
- One shared `Browser` instance per run, one `BrowserContext` per dealer (isolates cookies/storage).
- Set `user_agent` explicitly — do not use the default headless UA.
- Use `page.wait_for_selector` with explicit timeouts. No `time.sleep`.
- On any wait longer than 10s, log a warning.

### Timestamps
- **All internal timestamps are UTC** and timezone-aware (`datetime.now(timezone.utc)`).
- Scraped slot times are stored with their original timezone offset (dealer-local).
- Conversion happens at the analytics layer, never in the scraper.

### Error Handling
- Scrapers **never raise to the orchestrator**. They return a `ScrapeResult` with `status='error'` and `error_message` populated.
- Distinguish error types in the message prefix: `TIMEOUT:`, `PARSE:`, `NAVIGATION:`, `UNEXPECTED:`.
- Parse errors should include the fixture path or a snippet of the HTML that failed to parse.

### Logging
- Use `structlog` with JSON output.
- Every log line includes `dealer_code` and `run_id` as bound context.
- Log levels: `DEBUG` for internal state, `INFO` for milestones (run start, dealer complete), `WARNING` for recoverable issues, `ERROR` for dealer failures, `CRITICAL` only for run-level failures.

## Testing Conventions

- `pytest` with `pytest-asyncio` in auto mode.
- **Fixture-based parser tests**: HTML snapshots live in `tests/fixtures/<platform>/<dealer_code>/<scenario>.html`. Parser tests load the fixture and assert structured output. These run in milliseconds.
- **Live-site tests** are marked `@pytest.mark.live` and skipped by default. Run with `pytest -m live` manually.
- Test names describe the scenario: `test_xtime_parses_slots_when_availability_exists`, not `test_xtime_1`.
- When a scraper breaks on a real dealer, **first action** is to save the broken HTML as a new fixture and write a failing test against it.

## Dependency Management

- Use `uv`. Add dependencies with `uv add`, not by editing `pyproject.toml` manually.
- Pin major versions; allow minor/patch updates.
- No dependency without a justification in the commit message.

## Git Conventions

- Branch per slice: `slice/01-registry-loader`, `slice/02-xtime-scraper`, etc.
- Commit messages in imperative mood: "Add Xtime platform detector" not "Added..."
- Never commit data files in `data/raw/` or `data/processed/` — they go to Drive, not Git. (`.gitignore` handles this.)
- Fixtures in `tests/fixtures/` **are** committed — they're test assets.

## Secrets

- Drive service account JSON goes in `~/.config/vw-scraper/service_account.json`, never in the repo.
- Read paths from env vars: `VW_SCRAPER_SA_PATH`, `VW_SCRAPER_DRIVE_FOLDER_ID`.
- `.env.example` is committed; `.env` is not.

## What to Ask Me Before Doing

- Changing the observation record schema (see SPEC.md).
- Adding a new top-level dependency.
- Changing the Drive folder structure.
- Anything that would require backfilling historical data.

## What You Can Do Without Asking

- Refactor within a single file.
- Add tests.
- Improve logging.
- Fix bugs with tests that demonstrate the fix.
- Update this file or SPEC.md with proposed changes clearly marked as proposals.
