"""Tekion service scheduler scraper."""

import re

from config import PAGE_TIMEOUT, ACTION_TIMEOUT
from scrapers.base import BaseScraper, BlockedError


class TekionScraper(BaseScraper):
    """Scraper for Tekion-powered dealer service schedulers.

    Tekion schedulers are hosted at conscheduling.tekioncloud.com
    with dealer-specific access tokens.
    """

    async def _scrape_scheduler(self, dealer):
        url = dealer["url"]

        # Navigate to the Tekion scheduler
        await self.page.goto(url, timeout=PAGE_TIMEOUT, wait_until="networkidle")

        # Tekion requires phone or email sign-in first
        # Try to find a "Continue as Guest" or "Skip" option
        try:
            await self._try_guest_access()
        except Exception:
            # If no guest option, try entering a placeholder phone number
            try:
                await self._enter_phone_signin()
            except Exception as e:
                raise BlockedError(
                    f"Tekion requires sign-in and no guest option found: {e}"
                )

        # Wait for the scheduler to load past sign-in
        await self.page.wait_for_timeout(2000)

        # Try to enter VIN or select vehicle
        try:
            await self._enter_vehicle_info()
        except Exception:
            pass  # Some flows skip VIN entry

        # Select oil change service
        await self._select_oil_change()

        # Wait for calendar/time slots to load
        await self.page.wait_for_timeout(2000)

        # Extract earliest available date/time
        return await self._extract_earliest_slot()

    async def _try_guest_access(self):
        """Try to continue as guest without signing in."""
        guest_patterns = [
            "Continue as Guest",
            "Guest",
            "Skip",
            "Continue without",
            "No thanks",
        ]
        for pattern in guest_patterns:
            try:
                locator = self.page.get_by_text(pattern, exact=False).first
                if await locator.is_visible(timeout=3000):
                    await locator.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1000)
                    return
            except Exception:
                continue
        raise Exception("No guest access option found")

    async def _enter_phone_signin(self):
        """Enter a placeholder phone for sign-in."""
        # Look for phone input
        phone_input = self.page.locator(
            'input[type="tel"], input[placeholder*="phone" i], '
            'input[name*="phone" i], input[aria-label*="phone" i]'
        ).first
        if await phone_input.is_visible(timeout=3000):
            raise BlockedError(
                "Tekion requires phone verification — cannot proceed without a real phone number"
            )
        raise Exception("No phone input found")

    async def _enter_vehicle_info(self):
        """Enter VIN or select vehicle year/make/model."""
        # Try VIN input first
        vin_input = self.page.locator(
            'input[placeholder*="VIN" i], input[name*="vin" i], '
            'input[aria-label*="VIN" i], input[id*="vin" i]'
        ).first
        try:
            if await vin_input.is_visible(timeout=5000):
                await vin_input.fill(self.vin, timeout=ACTION_TIMEOUT)
                # Look for a submit/next button
                await self._click_next_or_submit()
                await self.page.wait_for_timeout(2000)
                return
        except Exception:
            pass

        # Try year/make/model selection
        try:
            # Select year
            year_select = self.page.locator(
                'select[name*="year" i], [aria-label*="year" i]'
            ).first
            if await year_select.is_visible(timeout=3000):
                await year_select.select_option(label="2023")
                await self.page.wait_for_timeout(500)

                # Select make
                make_select = self.page.locator(
                    'select[name*="make" i], [aria-label*="make" i]'
                ).first
                if await make_select.is_visible(timeout=3000):
                    await make_select.select_option(label="Volkswagen")
                    await self.page.wait_for_timeout(500)

                await self._click_next_or_submit()
                await self.page.wait_for_timeout(2000)
        except Exception:
            pass

    async def _select_oil_change(self):
        """Select oil change from the service menu."""
        oil_patterns = [
            "Oil Change",
            "Oil change",
            "oil change",
            "Lube, Oil",
            "Oil & Filter",
            "Oil and Filter",
            "Synthetic Oil",
            "Conventional Oil",
            "Oil Service",
        ]
        for pattern in oil_patterns:
            try:
                locator = self.page.get_by_text(pattern, exact=False).first
                if await locator.is_visible(timeout=3000):
                    await locator.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1000)
                    return
            except Exception:
                continue

        # Try clicking checkboxes or radio buttons near oil change text
        try:
            oil_label = self.page.locator(
                'label:has-text("oil change"), label:has-text("Oil Change")'
            ).first
            if await oil_label.is_visible(timeout=3000):
                await oil_label.click(timeout=ACTION_TIMEOUT)
                await self.page.wait_for_timeout(1000)
                return
        except Exception:
            pass

        raise Exception("Could not find oil change service option")

    async def _extract_earliest_slot(self):
        """Extract the earliest available date and time from the calendar."""
        # Look for available date buttons/slots
        # Tekion typically shows a calendar with clickable dates
        available_slots = self.page.locator(
            'button:not([disabled]):not([aria-disabled="true"])'
            '[class*="available" i], '
            '[class*="slot" i]:not([class*="unavailable" i]):not([class*="disabled" i]), '
            '[data-testid*="date" i]:not([disabled]), '
            '[class*="calendar" i] button:not([disabled])'
        )

        try:
            count = await available_slots.count()
            if count > 0:
                first_slot = available_slots.first
                slot_text = await first_slot.text_content(timeout=ACTION_TIMEOUT)

                # Click the first available slot to see time options
                await first_slot.click(timeout=ACTION_TIMEOUT)
                await self.page.wait_for_timeout(1000)

                # Try to extract date from the slot
                date = self._parse_date(slot_text)

                # Look for time slots
                time = await self._extract_time()

                return date or slot_text.strip(), time
        except Exception:
            pass

        # Fallback: look for any date-like text on the page
        page_text = await self.page.text_content("body")
        date_match = re.search(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s*\d{4})?)",
            page_text,
        )
        if date_match:
            return date_match.group(1), ""

        raise Exception("Could not find available appointment slots")

    async def _extract_time(self):
        """Extract the earliest available time slot."""
        time_slots = self.page.locator(
            '[class*="time" i] button:not([disabled]), '
            '[data-testid*="time" i]:not([disabled]), '
            'button:has-text(/\\d{1,2}:\\d{2}/)'
        )
        try:
            count = await time_slots.count()
            if count > 0:
                first_time = time_slots.first
                return (await first_time.text_content(timeout=ACTION_TIMEOUT)).strip()
        except Exception:
            pass

        # Look for time text in the page
        page_text = await self.page.text_content("body")
        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", page_text)
        if time_match:
            return time_match.group(1)

        return ""

    async def _click_next_or_submit(self):
        """Click the next/continue/submit button."""
        patterns = ["Next", "Continue", "Submit", "Search", "Find", "Look Up"]
        for pattern in patterns:
            try:
                btn = self.page.get_by_role("button", name=re.compile(pattern, re.I)).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=ACTION_TIMEOUT)
                    return
            except Exception:
                continue

    def _parse_date(self, text):
        """Try to extract a date from text."""
        if not text:
            return ""
        # Match patterns like "March 15", "Mar 15, 2024", "3/15/2024"
        patterns = [
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s*\d{4})?)",
            r"(\d{1,2}/\d{1,2}/\d{2,4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return ""
