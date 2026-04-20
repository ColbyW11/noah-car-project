"""Daily-run orchestrator.

Dispatches every active dealer in the registry to the scraper that matches its
platform, runs them concurrently under a semaphore, and writes one JSONL line
per dealer plus a `run_metadata.json` to `<output_dir>/YYYY-MM-DD/`.

Design notes worth keeping in mind:

- Per-dealer failures never abort the run (SPEC.md principle #3). Any exception
  that escapes `scrape()` (contract violation) is caught here and turned into a
  loud `UNEXPECTED:` error `ScrapeResult`.
- The orchestrator-level timeout (`_HARD_TIMEOUT_SECONDS = 65`) is 5s longer
  than the xtime scraper's internal 60s cap so a well-behaved scraper's own
  timeout fires first and returns a structured `ScrapeResult` with partial
  state. Orchestrator timeout only triggers on a misbehaving scraper.
- Writes are atomic (tempfile + fsync + os.replace) so a re-run on the same
  date cleanly replaces the partition — SPEC.md principle #5.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Browser, async_playwright
from pydantic import BaseModel, ConfigDict

from . import __version__
from .models import ScrapeResult, ScrapeStatus
from .registry import DealerConfig, Platform, load_registry
from .scrapers.base import PlatformScraper
from .scrapers.xtime import XtimeScraper

log = structlog.get_logger()

_HARD_TIMEOUT_SECONDS: float = 65.0


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    observation_date: date
    start_ts: datetime
    end_ts: datetime
    duration_seconds: float
    dealers_attempted: int
    success_count: int
    error_count: int
    scraper_version: str
    concurrency: int


def _default_scraper_map() -> dict[Platform, PlatformScraper]:
    return {Platform.XTIME: XtimeScraper()}


async def run_daily(
    registry_path: Path,
    output_dir: Path,
    concurrency: int = 5,
    *,
    scraper_map: Mapping[Platform, PlatformScraper] | None = None,
    browser: Browser | None = None,
    now: Callable[[], datetime] | None = None,
    headed: bool = False,
) -> RunMetadata:
    """Scrape every active dealer, write raw JSONL + run metadata.

    Returns a `RunMetadata` describing the run; also written to disk as JSON.
    Never raises for per-dealer failures — those are recorded in the JSONL as
    error ScrapeResults. Only raises for registry-loading failures (which mean
    we can't even start) or I/O failures writing the output files.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    effective_now = now or (lambda: datetime.now(timezone.utc))
    effective_scrapers: Mapping[Platform, PlatformScraper] = (
        scraper_map if scraper_map is not None else _default_scraper_map()
    )

    dealers = load_registry(registry_path)
    run_id = uuid.uuid4().hex
    start_ts = effective_now()
    observation_date = start_ts.date()
    bound_log = log.bind(run_id=run_id, observation_date=observation_date.isoformat())

    bound_log.info(
        "run_start",
        dealers_attempted=len(dealers),
        concurrency=concurrency,
    )

    t0 = time.monotonic()

    owned_browser = False
    playwright_ctx: Any = None
    active_browser = browser
    try:
        if active_browser is None:
            playwright_ctx = await async_playwright().start()
            active_browser = await playwright_ctx.chromium.launch(headless=not headed)
            owned_browser = True

        results = await _dispatch_all(
            dealers=dealers,
            scraper_map=effective_scrapers,
            browser=active_browser,
            observation_ts=start_ts,
            concurrency=concurrency,
            bound_log=bound_log,
        )
    finally:
        if owned_browser and active_browser is not None:
            try:
                await active_browser.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                bound_log.warning("browser_close_failed", tb=traceback.format_exc())
        if playwright_ctx is not None:
            try:
                await playwright_ctx.stop()
            except Exception:  # noqa: BLE001
                bound_log.warning("playwright_stop_failed", tb=traceback.format_exc())

    end_ts = effective_now()
    duration_seconds = time.monotonic() - t0
    success_count = sum(1 for r in results if r.scrape_status is ScrapeStatus.SUCCESS)
    error_count = len(results) - success_count

    metadata = RunMetadata(
        run_id=run_id,
        observation_date=observation_date,
        start_ts=start_ts,
        end_ts=end_ts,
        duration_seconds=duration_seconds,
        dealers_attempted=len(dealers),
        success_count=success_count,
        error_count=error_count,
        scraper_version=__version__,
        concurrency=concurrency,
    )

    partition_dir = output_dir / observation_date.isoformat()
    partition_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        partition_dir / "observations.jsonl",
        "".join(result.model_dump_json() + "\n" for result in results),
    )
    _atomic_write_text(
        partition_dir / "run_metadata.json",
        metadata.model_dump_json(indent=2) + "\n",
    )

    bound_log.info(
        "run_done",
        duration_seconds=duration_seconds,
        success_count=success_count,
        error_count=error_count,
    )
    return metadata


async def _dispatch_all(
    dealers: list[DealerConfig],
    scraper_map: Mapping[Platform, PlatformScraper],
    browser: Browser,
    observation_ts: datetime,
    concurrency: int,
    bound_log: Any,
) -> list[ScrapeResult]:
    sem = asyncio.Semaphore(concurrency)

    async def _one(dealer: DealerConfig) -> ScrapeResult:
        async with sem:
            return await _run_one_dealer(
                dealer=dealer,
                scraper_map=scraper_map,
                browser=browser,
                observation_ts=observation_ts,
                bound_log=bound_log,
            )

    return list(await asyncio.gather(*(_one(d) for d in dealers)))


async def _run_one_dealer(
    dealer: DealerConfig,
    scraper_map: Mapping[Platform, PlatformScraper],
    browser: Browser,
    observation_ts: datetime,
    bound_log: Any,
) -> ScrapeResult:
    dealer_log = bound_log.bind(dealer_code=dealer.dealer_code, platform=dealer.platform.value)
    scraper = scraper_map.get(dealer.platform)
    if scraper is None:
        dealer_log.warning("no_scraper_for_platform")
        return _synthetic_error(
            dealer,
            observation_ts,
            f"UNEXPECTED: no scraper for platform={dealer.platform.value}",
        )

    try:
        return await asyncio.wait_for(
            scraper.scrape(dealer, browser), timeout=_HARD_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        dealer_log.error("orchestrator_hard_timeout", seconds=_HARD_TIMEOUT_SECONDS)
        return _synthetic_error(
            dealer,
            observation_ts,
            f"TIMEOUT: orchestrator hard cap exceeded ({_HARD_TIMEOUT_SECONDS}s)",
        )
    except Exception as exc:  # noqa: BLE001 — scraper contract violation
        dealer_log.error(
            "scraper_contract_violation",
            error=str(exc),
            tb=traceback.format_exc(),
        )
        return _synthetic_error(dealer, observation_ts, f"UNEXPECTED: {exc}")


def _synthetic_error(
    dealer: DealerConfig,
    observation_ts: datetime,
    error_message: str,
) -> ScrapeResult:
    return ScrapeResult(
        dealer_code=dealer.dealer_code,
        observation_ts=observation_ts,
        scrape_status=ScrapeStatus.ERROR,
        error_message=error_message,
        first_available_ts=None,
        lead_time_hours=None,
        available_slots=[],
        slot_count=0,
        scheduling_flow_seconds=None,
        interaction_steps=0,
        platform=dealer.platform,
        source_payload_hash=None,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically.

    Same tempfile+fsync+os.replace pattern used in scripts/discover_platforms.py
    to update the dealer registry. POSIX guarantees `os.replace` is atomic on
    the same filesystem, so readers either see the old file or the new one —
    never a truncated in-progress write. Cleans up the temp on any error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise
