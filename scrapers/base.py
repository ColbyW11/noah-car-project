"""Abstract base scraper for dealer service schedulers."""

import os
from abc import ABC, abstractmethod
from datetime import datetime

from config import SCREENSHOTS_DIR, ACTION_TIMEOUT


class BaseScraper(ABC):
    """Base class for dealer service scheduler scrapers."""

    def __init__(self, page, vin, headless=False):
        self.page = page
        self.vin = vin
        self.headless = headless

    async def scrape(self, dealer):
        """Scrape a dealer's scheduler and return result dict.

        Args:
            dealer: dict with keys: name, url, platform, state

        Returns:
            dict with keys: dealer_name, state, platform, earliest_date,
                           earliest_time, status, error, screenshot_path, url
        """
        result = {
            "dealer_name": dealer["name"],
            "state": dealer.get("state", ""),
            "platform": dealer["platform"],
            "earliest_date": "",
            "earliest_time": "",
            "status": "error",
            "error": "",
            "screenshot_path": "",
            "url": dealer["url"],
        }

        try:
            date, time = await self._scrape_scheduler(dealer)
            result["earliest_date"] = date
            result["earliest_time"] = time
            result["status"] = "success"
        except BlockedError as e:
            result["status"] = "blocked"
            result["error"] = str(e)
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        # Always try to take a screenshot
        try:
            result["screenshot_path"] = await self._take_screenshot(dealer["name"])
        except Exception:
            pass

        return result

    @abstractmethod
    async def _scrape_scheduler(self, dealer):
        """Platform-specific scraping logic.

        Args:
            dealer: dict with dealer info

        Returns:
            tuple: (earliest_date: str, earliest_time: str)

        Raises:
            BlockedError: if captcha/login wall encountered
            Exception: on any other failure
        """
        pass

    async def _take_screenshot(self, dealer_name):
        """Take a screenshot and save it to the screenshots directory."""
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        safe_name = dealer_name.replace(" ", "_").replace("/", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_{timestamp}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        await self.page.screenshot(path=filepath, full_page=True)
        return filepath

    async def _click_text(self, text, timeout=None):
        """Click an element containing the given text."""
        timeout = timeout or ACTION_TIMEOUT
        await self.page.get_by_text(text, exact=False).first.click(timeout=timeout)

    async def _fill_input(self, selector, value, timeout=None):
        """Fill an input field."""
        timeout = timeout or ACTION_TIMEOUT
        await self.page.locator(selector).first.fill(value, timeout=timeout)

    async def _wait_for_text(self, text, timeout=None):
        """Wait for text to appear on the page."""
        timeout = timeout or ACTION_TIMEOUT
        await self.page.get_by_text(text, exact=False).first.wait_for(
            state="visible", timeout=timeout
        )


class BlockedError(Exception):
    """Raised when scraping is blocked by captcha, login wall, etc."""
    pass
