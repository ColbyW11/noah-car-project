"""Xtime service scheduler scraper."""

import re

from config import PAGE_TIMEOUT, ACTION_TIMEOUT
from scrapers.base import BaseScraper, BlockedError


class XtimeScraper(BaseScraper):
    """Scraper for Xtime-powered dealer service schedulers.

    Xtime schedulers are typically embedded as iframes on dealer websites.
    """

    async def _scrape_scheduler(self, dealer):
        url = dealer["url"]

        # Navigate to the dealer's service scheduling page
        await self.page.goto(url, timeout=PAGE_TIMEOUT, wait_until="networkidle")

        # Check for captcha or access blocks
        await self._check_for_blocks()

        # Try to find and switch into the Xtime iframe
        frame = await self._find_scheduler_frame()

        # Use the frame (or main page if no iframe found)
        context = frame if frame else self.page

        # Enter vehicle info (VIN)
        await self._enter_vehicle_info(context)

        # Select oil change service
        await self._select_oil_change(context)

        # Navigate to calendar/appointment selection
        await self._navigate_to_calendar(context)

        # Extract earliest available date/time
        return await self._extract_earliest_slot(context)

    async def _check_for_blocks(self):
        """Check if the page has captcha or other blocks."""
        page_text = (await self.page.text_content("body") or "").lower()
        block_indicators = [
            "captcha",
            "verify you are human",
            "robot",
            "access denied",
            "403 forbidden",
            "blocked",
        ]
        for indicator in block_indicators:
            if indicator in page_text:
                raise BlockedError(f"Page appears blocked: found '{indicator}'")

    async def _find_scheduler_frame(self):
        """Find the Xtime scheduler iframe."""
        # Wait a moment for iframes to load
        await self.page.wait_for_timeout(2000)

        # Look for iframes that might contain the scheduler
        frames = self.page.frames
        for frame in frames:
            frame_url = frame.url.lower()
            if any(
                keyword in frame_url
                for keyword in ["xtime", "schedule", "service", "appointment"]
            ):
                return frame

        # Try finding iframe by common selectors
        iframe_selectors = [
            'iframe[src*="xtime"]',
            'iframe[src*="schedule"]',
            'iframe[src*="service"]',
            'iframe[id*="xtime" i]',
            'iframe[id*="schedule" i]',
            'iframe[class*="scheduler" i]',
        ]
        for selector in iframe_selectors:
            try:
                iframe_el = self.page.locator(selector).first
                if await iframe_el.is_visible(timeout=3000):
                    return await iframe_el.content_frame()
            except Exception:
                continue

        # No iframe found — scheduler might be directly on the page
        return None

    async def _enter_vehicle_info(self, context):
        """Enter VIN or vehicle information."""
        # Try VIN input
        vin_selectors = [
            'input[placeholder*="VIN" i]',
            'input[name*="vin" i]',
            'input[id*="vin" i]',
            'input[aria-label*="VIN" i]',
            'input[data-testid*="vin" i]',
        ]
        for selector in vin_selectors:
            try:
                vin_input = context.locator(selector).first
                if await vin_input.is_visible(timeout=3000):
                    await vin_input.fill(self.vin, timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(500)
                    # Submit VIN
                    await self._click_next_or_submit(context)
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

        # Try year/make/model dropdowns as fallback
        try:
            await self._select_year_make_model(context)
        except Exception:
            pass

    async def _select_year_make_model(self, context):
        """Select vehicle by year/make/model dropdowns."""
        # Year
        year_select = context.locator(
            'select[name*="year" i], select[id*="year" i]'
        ).first
        try:
            if await year_select.is_visible(timeout=3000):
                await year_select.select_option(label="2023")
                await self.page.wait_for_timeout(500)
        except Exception:
            return

        # Make
        make_select = context.locator(
            'select[name*="make" i], select[id*="make" i]'
        ).first
        try:
            if await make_select.is_visible(timeout=3000):
                await make_select.select_option(label="Volkswagen")
                await self.page.wait_for_timeout(500)
        except Exception:
            pass

        # Model
        model_select = context.locator(
            'select[name*="model" i], select[id*="model" i]'
        ).first
        try:
            if await model_select.is_visible(timeout=3000):
                await model_select.select_option(index=1)  # Select first available model
                await self.page.wait_for_timeout(500)
        except Exception:
            pass

        await self._click_next_or_submit(context)
        await self.page.wait_for_timeout(2000)

    async def _select_oil_change(self, context):
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
            "Express Service",
        ]

        for pattern in oil_patterns:
            try:
                locator = context.get_by_text(pattern, exact=False).first
                if await locator.is_visible(timeout=3000):
                    await locator.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1000)
                    # Try clicking next after selecting service
                    try:
                        await self._click_next_or_submit(context)
                        await self.page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    return
            except Exception:
                continue

        # Try checkboxes/radio buttons
        try:
            checkbox = context.locator(
                'input[type="checkbox"][value*="oil" i], '
                'input[type="radio"][value*="oil" i]'
            ).first
            if await checkbox.is_visible(timeout=3000):
                await checkbox.check(timeout=ACTION_TIMEOUT)
                await self.page.wait_for_timeout(500)
                await self._click_next_or_submit(context)
                return
        except Exception:
            pass

        raise Exception("Could not find oil change service option")

    async def _navigate_to_calendar(self, context):
        """Navigate to the date/time selection step."""
        # Sometimes there's a transportation/advisor step between service and calendar
        # Try to skip through it
        skip_patterns = ["Next", "Continue", "Skip", "No Thanks", "Any Advisor"]
        for pattern in skip_patterns:
            try:
                btn = context.get_by_text(pattern, exact=False).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1500)
            except Exception:
                continue

    async def _extract_earliest_slot(self, context):
        """Extract the earliest available date and time."""
        # Look for available date elements in calendar
        date_selectors = [
            'td:not(.disabled):not(.unavailable) a',
            'button:not([disabled])[class*="day" i]',
            '[class*="available" i][class*="day" i]',
            '[class*="calendar" i] td:not(.disabled) button',
            '[data-available="true"]',
            '.day:not(.disabled):not(.past)',
        ]

        for selector in date_selectors:
            try:
                slots = context.locator(selector)
                count = await slots.count()
                if count > 0:
                    first_slot = slots.first
                    slot_text = await first_slot.text_content(timeout=ACTION_TIMEOUT)

                    # Click it to load time slots
                    await first_slot.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1500)

                    # Try to get the full date context
                    date = await self._get_full_date(context, slot_text)

                    # Extract time
                    time = await self._extract_time(context)

                    return date, time
            except Exception:
                continue

        # Fallback: look for date text on page
        page_text = await context.text_content("body") or ""
        date_match = re.search(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s*\d{4})?)",
            page_text,
        )
        if date_match:
            return date_match.group(1), ""

        # Try MM/DD/YYYY format
        date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", page_text)
        if date_match:
            return date_match.group(1), ""

        raise Exception("Could not find available appointment slots")

    async def _get_full_date(self, context, day_text):
        """Try to get the full date from calendar header + day number."""
        # Look for month/year header in calendar
        header_selectors = [
            '[class*="month" i]',
            '[class*="calendar" i] [class*="header" i]',
            '[class*="calendar" i] [class*="title" i]',
            "th[colspan]",
            ".ui-datepicker-title",
        ]
        for selector in header_selectors:
            try:
                header = context.locator(selector).first
                if await header.is_visible(timeout=2000):
                    header_text = await header.text_content(timeout=ACTION_TIMEOUT)
                    if header_text:
                        return f"{header_text.strip()} {day_text.strip()}"
            except Exception:
                continue
        return day_text.strip() if day_text else ""

    async def _extract_time(self, context):
        """Extract the earliest available time slot."""
        time_selectors = [
            'button:has-text(/\\d{1,2}:\\d{2}/)',
            '[class*="time" i] button:not([disabled])',
            '[class*="slot" i]:not([class*="unavailable" i])',
            '[data-testid*="time" i]',
        ]
        for selector in time_selectors:
            try:
                time_slots = context.locator(selector)
                count = await time_slots.count()
                if count > 0:
                    first_time = time_slots.first
                    text = await first_time.text_content(timeout=ACTION_TIMEOUT)
                    if text:
                        return text.strip()
            except Exception:
                continue

        # Fallback: regex for time pattern
        page_text = await context.text_content("body") or ""
        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", page_text)
        if time_match:
            return time_match.group(1)

        return ""

    async def _click_next_or_submit(self, context):
        """Click the next/continue/submit button."""
        patterns = ["Next", "Continue", "Submit", "Search", "Find", "Look Up", "Go"]
        for pattern in patterns:
            try:
                btn = context.get_by_role(
                    "button", name=re.compile(pattern, re.I)
                ).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=ACTION_TIMEOUT)
                    return
            except Exception:
                continue
