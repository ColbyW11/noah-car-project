"""One-shot: capture rendered Xtime slot data for parser fixtures.

Slice 2 fixtures only contain the entry/setup page — Xtime loads the actual
oil-change time slots via XHR after vehicle and service selection. Slice 3's
parser needs richer fixtures; this script produces them.

What it does:
  1. Navigates to VW0001's schedule_url (per data/dealer_master.csv).
  2. Hooks page.on("response") to capture every XHR/fetch response whose host
     contains "xtime" or "teamvelocity" — saves request URL + response body.
  3. Best-effort walks the form (selects a year/make/model if dropdowns exist,
     picks an oil-change-flavored service link), then waits for network idle.
  4. Writes:
       tests/fixtures/scrapers/xtime/slots_available/
         xhr_responses.jsonl   (one JSON line per captured response)
         schedule_page.html    (final rendered HTML)
         metadata.json         (capture timestamp, URL, what we clicked)

Run once, commit fixtures, never run again in CI. Live tests come in Slice 4.

Usage:
    uv run python scripts/capture_xtime_fixture.py
    uv run python scripts/capture_xtime_fixture.py --headed     # see browser
    uv run python scripts/capture_xtime_fixture.py --manual     # human walks form
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from vw_scraper.http import USER_AGENT, RobotsCache
from vw_scraper.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "scrapers" / "xtime" / "slots_available"

NAVIGATION_TIMEOUT_MS = 30_000
POST_LOAD_WAIT_SECONDS = 8
MANUAL_WAIT_SECONDS = 240  # 4 min for a human to walk the form

XTIME_HOST_MARKERS = ("xtime", "teamvelocity")

log = structlog.get_logger()


@dataclass
class CapturedResponse:
    url: str
    method: str
    status: int
    request_headers: dict[str, str]
    response_headers: dict[str, str]
    body_text: str | None
    body_b64: str | None  # only when body isn't text-decodable
    captured_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "request_headers": self.request_headers,
            "response_headers": self.response_headers,
            "body_text": self.body_text,
            "body_b64": self.body_b64,
            "captured_at_utc": self.captured_at_utc,
        }


@dataclass
class CaptureMetadata:
    dealer_code: str
    schedule_url: str
    final_url: str = ""
    user_agent: str = USER_AGENT
    started_at_utc: str = ""
    finished_at_utc: str = ""
    interaction_steps: list[str] = field(default_factory=list)
    captured_response_count: int = 0
    error: str | None = None


def _is_xtime_host(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in XTIME_HOST_MARKERS)


async def _capture_response(response: Response, captured: list[CapturedResponse]) -> None:
    if not _is_xtime_host(response.url):
        return
    try:
        body_bytes = await response.body()
    except PlaywrightError:
        body_bytes = b""

    body_text: str | None
    body_b64: str | None = None
    try:
        body_text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        import base64

        body_text = None
        body_b64 = base64.b64encode(body_bytes).decode("ascii")

    captured.append(
        CapturedResponse(
            url=response.url,
            method=response.request.method,
            status=response.status,
            request_headers=dict(response.request.headers),
            response_headers=dict(response.headers),
            body_text=body_text,
            body_b64=body_b64,
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    )
    log.info(
        "xhr_captured",
        url=response.url,
        method=response.request.method,
        status=response.status,
        bytes=len(body_bytes),
    )


async def _try_pick_first(page: Page, selector: str, label: str, steps: list[str]) -> bool:
    """Click the first matching element if present. Records the step."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=2_500)
        await locator.click(timeout=2_500)
        steps.append(f"clicked: {label} ({selector})")
        log.info("interaction_step", label=label, selector=selector)
        return True
    except (PlaywrightTimeoutError, PlaywrightError):
        return False


async def _try_select_first_option(page: Page, selector: str, label: str, steps: list[str]) -> bool:
    """Select the second option (skipping a 'Select…' placeholder) of a <select>."""
    try:
        sel = page.locator(selector).first
        await sel.wait_for(state="visible", timeout=2_500)
        options = await sel.locator("option").all_inner_texts()
        index = 1 if len(options) > 1 else 0
        await sel.select_option(index=index, timeout=2_500)
        chosen = options[index] if options else ""
        steps.append(f"selected: {label} = {chosen!r} ({selector})")
        log.info("interaction_step", label=label, selector=selector, value=chosen)
        return True
    except (PlaywrightTimeoutError, PlaywrightError):
        return False


async def _walk_xtime_form(page: Page, steps: list[str]) -> None:
    """Best-effort: click through the common Xtime flow.

    Xtime widgets vary, so we attempt several common selectors. Anything that
    doesn't match silently no-ops. Manual mode (--manual) is the safety net.
    """
    # Year / Make / Model selects
    await _try_select_first_option(page, "select[name*='year' i]", "year", steps)
    await _try_select_first_option(page, "select[name*='make' i]", "make", steps)
    await _try_select_first_option(page, "select[name*='model' i]", "model", steps)

    # "Continue" / "Next" buttons
    for label, sel in [
        ("continue button", "button:has-text('Continue')"),
        ("next button", "button:has-text('Next')"),
        ("get started button", "button:has-text('Get Started')"),
        ("schedule service link", "a:has-text('Schedule Service')"),
    ]:
        if await _try_pick_first(page, sel, label, steps):
            await asyncio.sleep(1.5)

    # Oil change service tile
    for label, sel in [
        ("oil change tile", "*:has-text('Oil Change')"),
        ("oil + filter tile", "*:has-text('Oil & Filter')"),
        ("oil filter tile", "*:has-text('Oil and Filter')"),
    ]:
        if await _try_pick_first(page, sel, label, steps):
            await asyncio.sleep(1.5)
            break


async def run_capture(
    *,
    headed: bool,
    manual: bool,
) -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    dealers = {d.dealer_code: d for d in load_registry(REGISTRY_CSV)}
    dealer = dealers.get("VW0001")
    if dealer is None or not dealer.schedule_url:
        log.error("no_dealer_or_schedule_url", dealer="VW0001")
        return 1

    bound_log = log.bind(dealer_code=dealer.dealer_code, schedule_url=dealer.schedule_url)
    robots = RobotsCache()
    if not robots.is_allowed(dealer.schedule_url):
        bound_log.error("robots_disallow_schedule")
        return 1

    captured: list[CapturedResponse] = []
    meta = CaptureMetadata(
        dealer_code=dealer.dealer_code,
        schedule_url=dealer.schedule_url,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
    )

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=not headed)
        context: BrowserContext | None = None
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            # Capture relevant XHR/fetch responses for the entire session.
            page.on(
                "response",
                lambda response: asyncio.create_task(_capture_response(response, captured)),
            )

            bound_log.info("navigate")
            try:
                await page.goto(dealer.schedule_url, wait_until="networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                bound_log.warning("networkidle_timeout_falling_back")
                await page.goto(dealer.schedule_url, wait_until="domcontentloaded")
            meta.final_url = page.url

            if manual:
                bound_log.info(
                    "manual_mode_waiting",
                    seconds=MANUAL_WAIT_SECONDS,
                    instructions=(
                        "Walk the form in the browser. Pick a vehicle, choose oil change, "
                        "wait for time slots to render. Then close the page or wait for timeout."
                    ),
                )
                try:
                    await page.wait_for_event("close", timeout=MANUAL_WAIT_SECONDS * 1000)
                except PlaywrightTimeoutError:
                    bound_log.warning("manual_wait_timeout")
            else:
                await _walk_xtime_form(page, meta.interaction_steps)
                bound_log.info("post_walk_wait", seconds=POST_LOAD_WAIT_SECONDS)
                await asyncio.sleep(POST_LOAD_WAIT_SECONDS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    bound_log.warning("post_walk_networkidle_timeout")

            try:
                html = await page.content()
                (FIXTURE_DIR / "schedule_page.html").write_text(html)
            except PlaywrightError as exc:
                bound_log.warning("html_capture_failed", error=str(exc))

        except PlaywrightTimeoutError as exc:
            meta.error = f"TIMEOUT: {exc}"
            bound_log.error("timeout", error=str(exc))
        except PlaywrightError as exc:
            meta.error = f"NAVIGATION: {exc}"
            bound_log.error("navigation_error", error=str(exc))
        except Exception as exc:
            meta.error = f"UNEXPECTED: {exc}"
            bound_log.error("unexpected", error=str(exc), tb=traceback.format_exc())
        finally:
            if context is not None:
                await context.close()
            await browser.close()

    meta.captured_response_count = len(captured)
    meta.finished_at_utc = datetime.now(timezone.utc).isoformat()

    # Write all captured responses as JSONL (one line each); the parser will
    # pick the richest one. Keep the full set so we can re-pick if needed.
    with (FIXTURE_DIR / "xhr_responses.jsonl").open("w") as fh:
        for resp in captured:
            fh.write(json.dumps(resp.to_dict()) + "\n")

    (FIXTURE_DIR / "metadata.json").write_text(json.dumps(meta.__dict__, indent=2))

    log.info(
        "capture_done",
        captured=len(captured),
        fixture_dir=str(FIXTURE_DIR),
        error=meta.error,
    )
    return 0 if meta.error is None else 2


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Open headed and wait for human interaction (implies --headed).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_capture(headed=args.headed or args.manual, manual=args.manual))


if __name__ == "__main__":
    sys.exit(main())
