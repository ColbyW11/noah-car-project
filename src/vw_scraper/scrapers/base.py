"""PlatformScraper Protocol — the contract every per-platform scraper implements.

Defined in SPEC.md (lines 113–120). The orchestrator (Slice 5) will dispatch
each dealer to the matching scraper via a router (Slice 8) using this Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from playwright.async_api import Browser

from ..models import ScrapeResult
from ..registry import DealerConfig


@runtime_checkable
class PlatformScraper(Protocol):
    """Per-platform scraper contract.

    `scrape()` must never raise to the orchestrator — it returns a
    `ScrapeResult` with `scrape_status='error'` and `error_message` populated
    for any failure (SPEC.md: failure isolation, loud failure not silent drift).
    """

    platform_name: str

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult: ...
