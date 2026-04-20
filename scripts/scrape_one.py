"""CLI: run a single-dealer scrape and print the JSON ScrapeResult.

Debugging tool for Slice 4 — the full orchestrator (all dealers, concurrency,
JSONL writing, run metadata) comes in Slice 5. Exit code is 0 on a successful
scrape, 2 on scrape_status='error' (so shell scripts can branch), 1 on
invocation errors (missing dealer, no scraper for platform).

    uv run python scripts/scrape_one.py VW0001
    uv run python scripts/scrape_one.py VW0001 --headed    # watch the browser
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import structlog
from playwright.async_api import async_playwright

from vw_scraper.models import ScrapeResult, ScrapeStatus
from vw_scraper.registry import Platform, load_registry
from vw_scraper.scrapers.xtime import XtimeScraper

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"

log = structlog.get_logger()


def _configure_logging(debug: bool) -> None:
    # Route logs to stderr so stdout stays clean for JSON piping into jq.
    level = logging.DEBUG if debug else logging.INFO
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


async def _scrape(dealer_code: str, headed: bool) -> ScrapeResult | None:
    dealers = {d.dealer_code: d for d in load_registry(REGISTRY_CSV)}
    dealer = dealers.get(dealer_code)
    if dealer is None:
        log.error("unknown_dealer", dealer_code=dealer_code)
        return None

    if dealer.platform is not Platform.XTIME:
        log.error(
            "no_scraper_for_platform",
            dealer_code=dealer_code,
            platform=dealer.platform.value,
            hint="Only xtime is wired in Slice 4. Slice 8 adds the platform router.",
        )
        return None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        try:
            return await XtimeScraper().scrape(dealer, browser)
        finally:
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dealer_code", help="Dealer code in data/dealer_master.csv (e.g. VW0001)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG-level logs (shows every JSON XHR URL).")
    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)

    result = asyncio.run(_scrape(args.dealer_code, headed=args.headed))
    if result is None:
        return 1

    print(result.model_dump_json(indent=2))
    return 0 if result.scrape_status is ScrapeStatus.SUCCESS else 2


if __name__ == "__main__":
    sys.exit(main())
