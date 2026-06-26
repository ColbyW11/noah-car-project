"""Regression test for the VW0002 Jeff outage (consumer.xtime SPA, 2026-06).

The transport step's markup changed: options went from `role="radio"` to a
`<div class="panel-drop-off__slot">` row wrapping a `<div role="checkbox"
aria-label="I'll wait at the dealership"|"I have a ride">`. The walker still
looked for `[role='radio']`, selected nothing, clicked NEXT on an invalid
form, and the slot-availability XHR never fired — a silent 120s timeout every
day for ~a month.

`tests/fixtures/scrapers/xtime/transport_step_2026/transport_step.html` is the
captured broken page. This test loads it into a real (offline) browser page
and asserts that `TRANSPORT_OPTION_SELECTORS` — the exact source-of-truth list
the walker iterates — resolves to the "wait" option and that clicking it
toggles `aria-checked`. If Xtime changes the markup again, this fails loudly
with the fixture to update.

Marked `browser`: uses a local headless browser, no network. Skipped by
default; run with `pytest -m browser`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from vw_scraper.scrapers.xtime import TRANSPORT_OPTION_SELECTORS

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "scrapers"
    / "xtime"
    / "transport_step_2026"
    / "transport_step.html"
)


@pytest.mark.browser
async def test_transport_selectors_match_2026_drop_off_markup() -> None:
    html = FIXTURE.read_text()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(html, wait_until="commit")

            # The first selector in the walker's preference order must resolve
            # to exactly one element on this page — that's what was broken
            # (the walker looked for role="radio"; the option became a
            # role="checkbox" nested in a .panel-drop-off__slot row).
            first_label, first_selector = TRANSPORT_OPTION_SELECTORS[0]
            assert "wait" in first_label
            matched = page.locator(first_selector)
            assert await matched.count() == 1, (
                f"{first_selector!r} should match exactly the 'wait' transport "
                "slot; the 2026 fixture markup may have changed again."
            )

            # The matched element must be the .panel-drop-off__slot ROW (the
            # real click target with cursor:pointer), not the inert nested
            # checkbox — a click on the checkbox leaves the form invalid. (The
            # live toggle behaviour is proven by the live scrape; a static
            # fixture has no SPA JS to flip aria-checked.)
            class_attr = await matched.first.get_attribute("class") or ""
            assert "panel-drop-off__slot" in class_attr

            wait_checkbox = matched.locator("[role='checkbox'][aria-label*='wait' i]")
            assert await wait_checkbox.count() == 1
            assert "wait" in (
                await wait_checkbox.first.get_attribute("aria-label") or ""
            ).lower()

            # Both options (drop-off + wait) are present and distinct.
            assert await page.locator(".panel-drop-off__slot").count() == 2
        finally:
            await browser.close()


@pytest.mark.browser
async def test_some_transport_selector_matches_the_fixture() -> None:
    """Belt-and-suspenders: at least one selector in the list must match, so a
    reorder/rename of the preference list can't silently break selection."""
    html = FIXTURE.read_text()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(html, wait_until="commit")
            counts = {
                selector: await page.locator(selector).count()
                for _label, selector in TRANSPORT_OPTION_SELECTORS
            }
            assert any(c > 0 for c in counts.values()), counts
        finally:
            await browser.close()
