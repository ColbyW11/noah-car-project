"""ConnectCDK scraper.

VW0005 (vwnanuet.com) loads the ConnectCDK / VW SHIFT scheduler in an iframe
served from `api.connectcdk.com`. The iframe's React app talks to the CDK
microservice host `nc-cdk-service-cosa-microservice.na.connectcdk.com` for
dealer config and (eventually) availability data.

Unlike Xtime's `{success, code, message, items}` envelope, ConnectCDK tends
to return raw top-level JSON — captured endpoints like `/Teams` and
`/GetDealerFeatureSettings` return lists directly; `/DealerInfo`, `/Settings`
return plain objects. The parser below accepts either form: a bare list of
slot dicts, or a dict with a slot-list under `slots` / `availableSlots` /
`appointments` / `items`.

The live walk drives the 5-step React wizard (Vehicle → Service → Time →
Contact → Confirm). MDC text-fields don't open with a plain `.click()` —
they're autocomplete-style; we `.fill()` to filter, then click the matching
`<li>`. Slot data lands when the user picks a date on step 3, via a
`/Availability` (or similar) XHR our generic JSON sniffer catches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
)

from ..http import USER_AGENT, RobotsCache
from ..models import ScrapeResult, ScrapeStatus
from ..registry import DealerConfig, Platform

log = structlog.get_logger()

DEALER_TIMEOUT_SECONDS = 120
NAVIGATION_TIMEOUT_MS = 30_000
VIEWPORT: ViewportSize = {"width": 1280, "height": 1800}
SLOT_BUDGET_SAFETY_SECONDS = 2.0
MAX_BODY_BYTES_FOR_PARSE = 1_000_000

DUMMY_MILES = "50000"


class ConnectCdkParseError(Exception):
    """Raised when a ConnectCDK availability payload cannot be parsed."""


_SLOT_LIST_KEYS = ("availableSlots", "slots", "appointments", "items", "timeslots", "timeSlots")
_SLOT_TIME_KEYS = (
    "startDateTime",
    "appointmentDateTime",
    "slotStart",
    "startTime",
    "dateTime",
    "time",
)

_LOGIN_WALL_MARKERS = (
    "please sign in to continue",
    "enter the code we sent",
    "temporary access code",
    "otp verification required",
    "you must sign in",
)

_TRAILING_Z = re.compile(r"Z$")


def parse_slots_from_payload(
    payload: Any, dealer_tz: str | None = None
) -> list[datetime]:
    """Extract slot datetimes from a ConnectCDK availability payload.

    Accepts either:
      - A bare list of slot dicts.
      - A dict with the slot list under one of `availableSlots` / `slots` /
        `appointments` / `items` / `timeslots` / `timeSlots`.

    Slot dicts have an ISO-8601 timestamp under `startDateTime` /
    `appointmentDateTime` / `slotStart` / `startTime` / `dateTime` / `time`.
    Bare slot strings are also accepted (CDK sometimes returns
    `timeSlots: ["2026-05-13T08:15:00-04:00", ...]`).

    When a slot string is wall-clock-only (no tz offset), `dealer_tz`
    (e.g. `"America/New_York"`) localizes it; otherwise we assume UTC.
    """
    items = _extract_slot_list(payload)
    slots: list[datetime] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            slots.append(_parse_iso_datetime(item, dealer_tz))
            continue
        if not isinstance(item, dict):
            raise ConnectCdkParseError(
                f"PARSE: items[{index}] is not a dict or string "
                f"(got {type(item).__name__})"
            )
        ts_str = _first_present_string(item, _SLOT_TIME_KEYS)
        if ts_str is None:
            raise ConnectCdkParseError(
                f"PARSE: items[{index}] has none of {_SLOT_TIME_KEYS}; "
                f"keys present: {sorted(item.keys())}"
            )
        slots.append(_parse_iso_datetime(ts_str, dealer_tz))
    slots.sort()
    return slots


def detect_login_wall(html: str) -> bool:
    """Return True when the rendered iframe gates slot access behind sign-in/OTP."""
    lowered = html.lower()
    return any(marker in lowered for marker in _LOGIN_WALL_MARKERS)


def _extract_slot_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _SLOT_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        raise ConnectCdkParseError(
            f"PARSE: dict payload missing any of {_SLOT_LIST_KEYS}; "
            f"keys present: {sorted(payload.keys())}"
        )
    raise ConnectCdkParseError(
        f"PARSE: expected list or dict payload, got {type(payload).__name__}"
    )


def _first_present_string(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_iso_datetime(value: str, dealer_tz: str | None = None) -> datetime:
    normalized = _TRAILING_Z.sub("+00:00", value)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConnectCdkParseError(
            f"PARSE: invalid ISO-8601 timestamp {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(dealer_tz) if dealer_tz else timezone.utc
        parsed = parsed.replace(tzinfo=tz)
    return parsed


@dataclass
class _ScrapeState:
    interaction_steps: int = 0
    scheduling_flow_seconds: float | None = None
    slots: list[datetime] = field(default_factory=list)
    source_payload_hash: str | None = None


def _looks_like_json_xhr(response: Response) -> bool:
    try:
        resource_type = response.request.resource_type
    except PlaywrightError:
        return False
    if resource_type not in ("xhr", "fetch"):
        return False
    content_type = response.headers.get("content-type", "").lower()
    return "json" in content_type


def _error_result(
    dealer: DealerConfig,
    observation_ts: datetime,
    message: str,
    state: _ScrapeState,
) -> ScrapeResult:
    return ScrapeResult(
        dealer_code=dealer.dealer_code,
        observation_ts=observation_ts,
        scrape_status=ScrapeStatus.ERROR,
        error_message=message,
        first_available_ts=None,
        lead_time_hours=None,
        available_slots=[],
        slot_count=0,
        scheduling_flow_seconds=state.scheduling_flow_seconds,
        interaction_steps=state.interaction_steps,
        platform=Platform.CONNECT_CDK,
        source_payload_hash=state.source_payload_hash,
    )


class ConnectCdkScraper:
    """PlatformScraper for the ConnectCDK / VW SHIFT scheduler.

    `scrape()` never raises to the caller — any exception (including
    `asyncio.TimeoutError` from the outer 120s cap) becomes a `ScrapeResult`
    with `scrape_status='error'` and a loud, prefixed `error_message`.
    """

    platform_name: str = Platform.CONNECT_CDK.value

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult:
        observation_ts = datetime.now(timezone.utc)
        state = _ScrapeState()
        bound_log = log.bind(dealer_code=dealer.dealer_code)

        try:
            return await asyncio.wait_for(
                self._scrape_inner(dealer, browser, observation_ts, state, bound_log),
                timeout=DEALER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            bound_log.error("scrape_hard_timeout", seconds=DEALER_TIMEOUT_SECONDS)
            return _error_result(
                dealer,
                observation_ts,
                f"TIMEOUT: exceeded {DEALER_TIMEOUT_SECONDS}s hard cap",
                state,
            )
        except Exception as exc:
            bound_log.error(
                "scrape_unexpected",
                error=str(exc),
                tb=traceback.format_exc(),
            )
            return _error_result(
                dealer,
                observation_ts,
                f"UNEXPECTED: {exc}",
                state,
            )

    async def _scrape_inner(
        self,
        dealer: DealerConfig,
        browser: Browser,
        observation_ts: datetime,
        state: _ScrapeState,
        bound_log: Any,
    ) -> ScrapeResult:
        robots = RobotsCache()
        if not robots.is_allowed(dealer.schedule_url):
            return _error_result(
                dealer,
                observation_ts,
                "NAVIGATION: robots.txt disallows schedule URL",
                state,
            )

        context: BrowserContext | None = None
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport=VIEWPORT,
            )
            # Apply playwright-stealth fingerprint patches. ConnectCDK's
            # React SPA at `/select-services` renders an empty body when
            # `navigator.webdriver === true` (or when other vanilla
            # Playwright fingerprints are present). Stealth patches all
            # the common detector hooks (webdriver flag, plugins,
            # chrome.runtime, permissions, etc.).
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(context)
            page = await context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            slot_future: asyncio.Future[tuple[list[datetime], bytes]] = (
                asyncio.get_running_loop().create_future()
            )
            dealer_tz = dealer.config_json.get("timezone")

            def _on_response(response: Response) -> None:
                asyncio.create_task(
                    _handle_response(response, slot_future, bound_log, dealer_tz)
                )

            page.on("response", _on_response)

            flow_start = time.monotonic()
            bound_log.info("navigate", schedule_url=dealer.schedule_url)
            try:
                await page.goto(dealer.schedule_url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as exc:
                return _error_result(
                    dealer, observation_ts, f"TIMEOUT: page.goto {exc}", state
                )
            except PlaywrightError as exc:
                return _error_result(
                    dealer, observation_ts, f"NAVIGATION: {exc}", state
                )

            # Resolve the connectcdk iframe.
            src_pattern = (
                dealer.config_json.get("iframe_src_pattern") or "api.connectcdk.com"
            )
            iframe_selector = f"iframe[src*='{src_pattern}']"
            try:
                await page.locator(iframe_selector).first.wait_for(
                    state="attached", timeout=20_000
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                return _error_result(
                    dealer,
                    observation_ts,
                    f"NAVIGATION: iframe {src_pattern} never attached",
                    state,
                )
            # Locate the Frame and wait for its body + SPA-ready signal.
            target_frame: Frame | None = None
            for _ in range(30):
                for f in page.frames:
                    if src_pattern in (f.url or ""):
                        target_frame = f
                        break
                if target_frame:
                    break
                await page.wait_for_timeout(500)
            if target_frame is None:
                return _error_result(
                    dealer,
                    observation_ts,
                    "NAVIGATION: connectcdk frame not found in page.frames",
                    state,
                )
            try:
                await target_frame.wait_for_selector("body", timeout=15_000)
                await target_frame.wait_for_selector(
                    "button.noSignOnCust-btn, button.repeatCust-btn",
                    timeout=15_000,
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                return _error_result(
                    dealer,
                    observation_ts,
                    "NAVIGATION: connectcdk SPA never rendered NEW/RETURNING CUSTOMER",
                    state,
                )
            bound_log.info("entered_iframe", iframe_src_pattern=src_pattern)

            await _walk_connect_cdk(target_frame, page, dealer, state, bound_log)

            elapsed = time.monotonic() - flow_start
            remaining = DEALER_TIMEOUT_SECONDS - elapsed - SLOT_BUDGET_SAFETY_SECONDS
            if remaining <= 0:
                return _error_result(
                    dealer,
                    observation_ts,
                    "TIMEOUT: no budget left for slot wait after form walk",
                    state,
                )

            try:
                slots, payload_bytes = await asyncio.wait_for(
                    slot_future, timeout=remaining
                )
            except asyncio.TimeoutError:
                return _error_result(
                    dealer,
                    observation_ts,
                    f"TIMEOUT: no slot XHR within {remaining:.1f}s",
                    state,
                )

            state.scheduling_flow_seconds = time.monotonic() - flow_start
            state.slots = slots
            state.source_payload_hash = (
                "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
            )

            bound_log.info(
                "scrape_success",
                slot_count=len(slots),
                flow_seconds=state.scheduling_flow_seconds,
                interaction_steps=state.interaction_steps,
            )

            first = slots[0]
            return ScrapeResult(
                dealer_code=dealer.dealer_code,
                observation_ts=observation_ts,
                scrape_status=ScrapeStatus.SUCCESS,
                error_message=None,
                first_available_ts=first,
                lead_time_hours=(first - observation_ts).total_seconds() / 3600,
                available_slots=slots,
                slot_count=len(slots),
                scheduling_flow_seconds=state.scheduling_flow_seconds,
                interaction_steps=state.interaction_steps,
                platform=Platform.CONNECT_CDK,
                source_payload_hash=state.source_payload_hash,
            )
        except ConnectCdkParseError as exc:
            return _error_result(dealer, observation_ts, str(exc), state)
        finally:
            if context is not None:
                try:
                    await context.close()
                except PlaywrightError:
                    pass


async def _handle_response(
    response: Response,
    slot_future: asyncio.Future[tuple[list[datetime], bytes]],
    bound_log: Any,
    dealer_tz: str | None = None,
) -> None:
    """Parse any JSON XHR; resolve slot_future on first slot hit.

    ConnectCDK's microservice host is `nc-cdk-service-cosa-microservice.na.connectcdk.com`
    and slot data fires from there once a date is selected on step 3. We
    don't pin the URL so the same handler also catches dealer-proxied
    variants (some dealers may proxy CDK through their own domain).
    """
    if slot_future.done():
        return
    if not _looks_like_json_xhr(response):
        return
    try:
        body_bytes = await response.body()
    except PlaywrightError:
        return
    if len(body_bytes) > MAX_BODY_BYTES_FOR_PARSE:
        return
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    bound_log.debug("json_xhr_seen", url=response.url, status=response.status)
    try:
        slots = parse_slots_from_payload(payload, dealer_tz=dealer_tz)
    except ConnectCdkParseError:
        return
    if slots and not slot_future.done():
        slot_future.set_result((slots, body_bytes))
        bound_log.info("slot_xhr_captured", url=response.url, slot_count=len(slots))


async def _walk_connect_cdk(
    frame: Frame,
    page: Page,
    dealer: DealerConfig,
    state: _ScrapeState,
    bound_log: Any,
) -> None:
    """Drive the ConnectCDK 5-step wizard: Vehicle → Service → Time → Contact → Confirm.

    Slot data fires when the Time step renders (typically right after the
    Service step continue), via an `/Availability` XHR from the microservice
    host. We pick a date if necessary to trigger that XHR.
    """
    # Step 0: choose NEW CUSTOMER (no existing-customer search).
    if not await _try_click(frame, "button.noSignOnCust-btn", "new customer", state, bound_log):
        bound_log.warning("connectcdk_new_customer_not_found")
        return
    await asyncio.sleep(2.5)

    # Step 1: Vehicle picker. MDC text-fields behave as autocomplete inputs —
    # filling the input filters a dropdown list of `<li>` options. Volkswagen
    # is preselected on VW0005 (vwnanuet), but generic handling tries both
    # Makes and Years/Models for portability across CDK dealers.
    await _try_fill_and_pick(
        frame, "#Makes", "Volkswagen", "make: VOLKSWAGEN", state, bound_log
    )
    await asyncio.sleep(1.0)

    # Pick a recent year; CDK lists newest first.
    if not await _try_fill_and_pick(frame, "#Years", "2026", "year: 2026", state, bound_log):
        await _try_fill_and_pick(frame, "#Years", "2025", "year: 2025", state, bound_log)
    await asyncio.sleep(1.0)

    # Models — fill with a known popular model or just pick the first option.
    # ATLAS is a common VW model and seen in real-traffic captures.
    if not await _try_fill_and_pick(frame, "#Models", "ATLAS", "model: ATLAS", state, bound_log):
        await _try_fill_and_pick(frame, "#Models", "JETTA", "model: JETTA", state, bound_log)
    await asyncio.sleep(1.0)

    # Mileage is a plain text input.
    await _try_fill(frame, "#mileage", DUMMY_MILES, "mileage", state, bound_log)
    await asyncio.sleep(1.0)

    # Continue to Services step.
    await _try_click(frame, "#next-button", "next: vehicle → services", state, bound_log)
    await asyncio.sleep(3.0)

    # Step 2: Service selection. ConnectCDK shows a tile/list of services
    # with checkboxes — Oil Change is the target.
    for label, selector in (
        ("service oil change tile", "[class*='service']:has-text('Oil Change')"),
        ("service oil change li", "li:has-text('Oil Change')"),
        ("service oil change card", "div[class*='card']:has-text('Oil Change')"),
        ("service oil change checkbox", "label:has-text('Oil Change')"),
        ("service oil filter tile", "[class*='service']:has-text('Oil & Filter')"),
        ("service engine oil tile", "[class*='service']:has-text('Engine oil')"),
    ):
        if await _try_click(frame, selector, label, state, bound_log):
            await asyncio.sleep(2.0)
            break

    # Continue to Time step.
    await _try_click(frame, "#next-button", "next: services → time", state, bound_log)
    await asyncio.sleep(4.0)

    # Step 3: Time/date picker. Pick the first available date — slot XHR
    # often fires immediately on landing, but if not, a date click triggers
    # it. ConnectCDK uses `[role='button']`-style date cells.
    for label, selector in (
        ("date first available", "[class*='available'][class*='date']"),
        ("date first cell", "[class*='date-cell']:not([class*='disabled'])"),
        ("date calendar cell", "[role='gridcell']:not([aria-disabled='true'])"),
        ("date button enabled", "button[class*='date']:not([disabled])"),
    ):
        if await _try_click(frame, selector, label, state, bound_log):
            await asyncio.sleep(2.0)
            break


async def _try_click(
    frame: Frame, selector: str, label: str, state: _ScrapeState, bound_log: Any
) -> bool:
    try:
        loc = frame.locator(selector).first
        await loc.wait_for(state="visible", timeout=3_000)
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
        except (PlaywrightTimeoutError, PlaywrightError):
            pass
        try:
            await loc.click(timeout=3_000)
        except PlaywrightTimeoutError:
            await loc.dispatch_event("click", timeout=3_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_click", label=label)
    return True


async def _try_fill(
    frame: Frame, selector: str, value: str, label: str, state: _ScrapeState, bound_log: Any
) -> bool:
    try:
        loc = frame.locator(selector).first
        await loc.wait_for(state="visible", timeout=2_500)
        await loc.fill(value, timeout=2_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_fill", label=label)
    return True


async def _try_fill_and_pick(
    frame: Frame,
    input_selector: str,
    value: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
    dropdown_list_class: str = "filtered-list",
) -> bool:
    """Fill an MDC autocomplete input and click the matching `<li>` option.

    ConnectCDK's React + MDC autocomplete only commits a value when the
    user types real keystrokes (not via `input.value = '...'`). Playwright's
    `.fill()` bypasses React's synthetic event listener, so the dropdown
    "opens" visually but the next cascading field (Year, Model) stays
    `disabled`. Use `.press_sequentially` (real keyboard events) instead.

    The matching `<li>` is scoped to the SPA's `vehicle-*-filtered-list`
    container so we don't accidentally click a stepper `<li>` (Vehicle,
    Service(s), Time, Contact, Confirm) that also contains matching text.
    """
    try:
        loc = frame.locator(input_selector).first
        await loc.wait_for(state="visible", timeout=5_000)
        for _ in range(10):
            if not await loc.evaluate("e => e.disabled"):
                break
            await asyncio.sleep(0.4)
        else:
            bound_log.debug("step_fill_field_stayed_disabled", label=label)
            return False
        # Click and give React time to register focus before typing.
        await loc.click(timeout=3_000)
        await asyncio.sleep(1.0)
        # Set value via React-aware native setter so the SPA's onChange
        # fires; `.fill()` and `.press_sequentially()` both dropped the
        # first character in headless tests due to a focus race condition,
        # and the dropdown never filtered correctly.
        committed = await loc.evaluate(
            """
            (el, val) => {
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
              ).set;
              setter.call(el, val);
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              return el.value;
            }
            """,
            value,
        )
        if committed != value:
            bound_log.debug("step_fill_value_not_committed", label=label, got=committed)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        bound_log.debug("step_fill_failed", label=label, err=str(exc)[:120])
        return False
    state.interaction_steps += 1
    bound_log.debug("step_fill", label=label)
    # Wait for the filtered dropdown to render.
    await asyncio.sleep(1.2)
    picked = False
    try:
        # Scope the `<li>` lookup to the make/year/model filtered list so
        # we don't match a stepper li or other unrelated mdc-list-item.
        option = frame.locator(
            f"ul[class*='{dropdown_list_class}'] li:has-text('{value}'), "
            f"ul[class*='filtered-list'] li.mdc-list-item:has-text('{value}')"
        ).first
        await option.wait_for(state="visible", timeout=4_000)
        await option.click(timeout=3_000)
        state.interaction_steps += 1
        bound_log.debug("step_select_option", label=label, value=value)
        picked = True
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        bound_log.debug("step_select_option_failed", label=label, err=str(exc)[:120])
    # Press Enter as a fallback commit + close the dropdown.
    try:
        await loc.press("Enter")
        await asyncio.sleep(0.4)
    except PlaywrightError:
        pass
    await asyncio.sleep(0.5)
    return picked
