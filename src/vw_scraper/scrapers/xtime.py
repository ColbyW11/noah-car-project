"""Xtime scraper.

Parses Xtime's slot XHR envelope (pure functions) and wires the parser to a
live Playwright session to produce a `ScrapeResult` for a given dealer.

Xtime widgets render inside the dealer page (dealer.com / Vue) but load slot
data from `xtime.teamvelocityportal.com` via XHR using the envelope
`{success, code, message, items, errorMsgForEndUser}`. We capture every
xtime/teamvelocity response, try to parse each as a slot payload, and take
the first that yields ≥1 slot. This avoids hardcoding the slot endpoint URL
(we still don't have it confirmed — the entry page hits `/Xtime/Vehicle/Years`
first, and the slot endpoint fires later after vehicle+service selection).
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
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
)

from ..http import USER_AGENT, RobotsCache
from ..models import ScrapeResult, ScrapeStatus
from ..registry import DealerConfig, Platform

log = structlog.get_logger()

DEALER_TIMEOUT_SECONDS = 60
NAVIGATION_TIMEOUT_MS = 30_000
VIEWPORT: ViewportSize = {"width": 1280, "height": 900}
XTIME_HOST_MARKERS = ("xtime", "teamvelocity")
SLOT_BUDGET_SAFETY_SECONDS = 2.0

# Dummy registration data. SPEC.md line 144 permits dummy data where required
# to reach availability. Values are identifiably fake: example.com is reserved
# for docs/testing (RFC 2606); the 555-01xx block is reserved for fictitious
# numbers (NANP). The scraper will fill these only if a registration modal
# gates slot rendering; otherwise they're never submitted.
DUMMY_FIRSTNAME = "Test"
DUMMY_LASTNAME = "User"
DUMMY_EMAIL = "test@example.com"
DUMMY_PHONE = "5550100"


class XtimeParseError(Exception):
    """Raised when an Xtime XHR payload cannot be parsed.

    Caught at the scraper boundary and turned into a `ScrapeResult` with
    `scrape_status='error'` and a `PARSE:` error_message prefix
    (CLAUDE.md error-handling convention).
    """


# Slot timestamp lives under one of these keys in items[]. Order matters:
# we pick the first that exists. Inferred from Xtime's published widget
# documentation patterns; will be confirmed against a real slot response in
# Slice 4 and pruned to whatever the live API actually returns.
_SLOT_TIME_KEYS = (
    "startDateTime",
    "appointmentDateTime",
    "appointmentTime",
    "startTime",
    "dateTime",
    "slotDateTime",
)

# Conservative markers: only phrases that appear when login *gates* access,
# not when a sign-in feature happens to be present in the page DOM. Real
# Xtime pages embed registration modals + sign-in popups in dormant form, so
# matching on "signin-container" or `type="password"` produces false positives.
_LOGIN_WALL_MARKERS = (
    "sign in to continue",
    "please sign in to your account",
    "login-required",
    "you must sign in",
)


def parse_slots_from_payload(payload: dict[str, Any]) -> list[datetime]:
    """Extract slot datetimes from an Xtime XHR JSON envelope.

    Envelope shape (real, captured from /Xtime/Vehicle/Years):
        {"success": bool, "code": int|null, "message": str,
         "items": list, "errorMsgForEndUser": list}

    Returns slots in chronological order. Returns `[]` when the envelope is
    valid but reports no availability. Raises `XtimeParseError` when the
    envelope is malformed or the API itself reported an error.
    """
    if not isinstance(payload, dict):
        raise XtimeParseError(
            f"PARSE: expected dict envelope, got {type(payload).__name__}"
        )

    if "success" not in payload or "items" not in payload:
        raise XtimeParseError(
            "PARSE: envelope missing 'success' or 'items' keys "
            f"(got: {sorted(payload.keys())})"
        )

    if payload["success"] is not True:
        msg = payload.get("message") or payload.get("errorMsgForEndUser") or "unknown"
        raise XtimeParseError(f"PARSE: Xtime API reported failure: {msg}")

    items = payload["items"]
    if not isinstance(items, list):
        raise XtimeParseError(
            f"PARSE: 'items' must be a list, got {type(items).__name__}"
        )

    slots: list[datetime] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise XtimeParseError(
                f"PARSE: items[{index}] is not a dict (got {type(item).__name__})"
            )
        ts_str = _first_present_string(item, _SLOT_TIME_KEYS)
        if ts_str is None:
            raise XtimeParseError(
                f"PARSE: items[{index}] has none of {_SLOT_TIME_KEYS}; "
                f"keys present: {sorted(item.keys())}"
            )
        slots.append(_parse_iso_datetime(ts_str))

    slots.sort()
    return slots


def detect_login_wall(html: str) -> bool:
    """Return True when the rendered page is gating slot access behind sign-in."""
    lowered = html.lower()
    return any(marker in lowered for marker in _LOGIN_WALL_MARKERS)


def _first_present_string(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# Xtime sometimes emits trailing-Z UTC and sometimes a numeric offset like
# "-04:00". `datetime.fromisoformat` on Python 3.11+ handles both, but we
# normalize a trailing 'Z' first because older variants of the API include it.
_TRAILING_Z = re.compile(r"Z$")


def _parse_iso_datetime(value: str) -> datetime:
    normalized = _TRAILING_Z.sub("+00:00", value)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise XtimeParseError(f"PARSE: invalid ISO-8601 timestamp {value!r}") from exc


@dataclass
class _ScrapeState:
    """Partial scrape state held outside the inner coroutine.

    Exists so that when the outer 60s `asyncio.wait_for` cancels the inner
    task, we can still report `interaction_steps` and `scheduling_flow_seconds`
    we accumulated before the cancel.
    """

    interaction_steps: int = 0
    scheduling_flow_seconds: float | None = None
    slots: list[datetime] = field(default_factory=list)
    source_payload_hash: str | None = None


def _is_xtime_host(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in XTIME_HOST_MARKERS)


class XtimeScraper:
    """PlatformScraper for the Xtime / TeamVelocity oil-change widget.

    `scrape()` never raises to the caller — any exception (including
    `asyncio.TimeoutError` from the outer 60s cap) becomes a `ScrapeResult`
    with `scrape_status='error'` and a loud, prefixed `error_message`.
    """

    platform_name: str = Platform.XTIME.value

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
            page = await context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            # Future resolved by `_handle_response` when the first parseable
            # slot XHR arrives with ≥1 slot. Tuple is (slots, raw body bytes)
            # — we hash the raw bytes for `source_payload_hash`.
            slot_future: asyncio.Future[tuple[list[datetime], bytes]] = (
                asyncio.get_running_loop().create_future()
            )

            def _on_response(response: Response) -> None:
                # Playwright's page.on is sync; hop into async to read body.
                asyncio.create_task(_handle_response(response, slot_future, bound_log))

            page.on("response", _on_response)

            flow_start = time.monotonic()
            bound_log.info("navigate", schedule_url=dealer.schedule_url)
            try:
                await page.goto(dealer.schedule_url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as exc:
                return _error_result(
                    dealer,
                    observation_ts,
                    f"TIMEOUT: page.goto {exc}",
                    state,
                )
            except PlaywrightError as exc:
                return _error_result(
                    dealer,
                    observation_ts,
                    f"NAVIGATION: {exc}",
                    state,
                )

            # Fast-fail on login wall so we don't waste the 60s budget.
            if detect_login_wall(await page.content()):
                return _error_result(
                    dealer,
                    observation_ts,
                    "NAVIGATION: login wall detected",
                    state,
                )

            if dealer.config_json.get("vehicle_selection_required", True):
                await _walk_xtime_form(page, state, bound_log)

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
                platform=Platform.XTIME,
                source_payload_hash=state.source_payload_hash,
            )
        except XtimeParseError as exc:
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
) -> None:
    """Parse one network response; resolve `slot_future` on first slot hit.

    Silently ignores anything that isn't a parseable Xtime slot envelope —
    we can't know in advance which of the ~10 xtime/teamvelocity XHRs is the
    slot endpoint, so we try every one and let the parser filter.
    """
    if slot_future.done():
        return
    if not _is_xtime_host(response.url):
        return
    try:
        body_bytes = await response.body()
    except PlaywrightError:
        return
    try:
        body_text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return
    try:
        slots = parse_slots_from_payload(payload)
    except XtimeParseError:
        return
    if slots and not slot_future.done():
        slot_future.set_result((slots, body_bytes))
        bound_log.info("slot_xhr_captured", url=response.url, slot_count=len(slots))


async def _walk_xtime_form(page: Page, state: _ScrapeState, bound_log: Any) -> None:
    """Best-effort walk: vehicle → service → registration modal (dummy data).

    Every step is optional — if a selector doesn't match, we no-op and move
    on. The slot XHR may fire during any of these steps; `_handle_response`
    catches it regardless.
    """
    for label, selector in (
        ("year", "select[name*='year' i]"),
        ("make", "select[name*='make' i]"),
        ("model", "select[name*='model' i]"),
    ):
        if await _try_select_first_real_option(page, selector, label, state, bound_log):
            await asyncio.sleep(0.5)

    for label, selector in (
        ("continue button", "button:has-text('Continue')"),
        ("next button", "button:has-text('Next')"),
        ("get started button", "button:has-text('Get Started')"),
        ("schedule service link", "a:has-text('Schedule Service')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(1.0)

    for label, selector in (
        ("oil change tile", "*:has-text('Oil Change')"),
        ("oil + filter tile", "*:has-text('Oil & Filter')"),
        ("oil filter tile", "*:has-text('Oil and Filter')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(1.0)
            break

    filled_any = False
    for label, selector, value in (
        ("firstname", "input[name*='first' i], input[placeholder*='first' i]", DUMMY_FIRSTNAME),
        ("lastname", "input[name*='last' i], input[placeholder*='last' i]", DUMMY_LASTNAME),
        ("email", "input[type='email'], input[name*='email' i]", DUMMY_EMAIL),
        ("phone", "input[type='tel'], input[name*='phone' i]", DUMMY_PHONE),
    ):
        if await _try_fill(page, selector, value, label, state, bound_log):
            filled_any = True

    if filled_any:
        bound_log.info("registration_modal_filled_with_dummy_data")
        for label, selector in (
            ("registration continue", "button:has-text('Continue')"),
            ("registration next", "button:has-text('Next')"),
            ("registration submit", "button:has-text('Submit')"),
        ):
            if await _try_click(page, selector, label, state, bound_log):
                await asyncio.sleep(1.0)
                break


async def _try_click(
    page: Page,
    selector: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=2_500)
        await locator.click(timeout=2_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_click", label=label)
    return True


async def _try_fill(
    page: Page,
    selector: str,
    value: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=2_000)
        await locator.fill(value, timeout=2_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_fill", label=label)
    return True


async def _try_select_first_real_option(
    page: Page,
    selector: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    try:
        sel = page.locator(selector).first
        await sel.wait_for(state="visible", timeout=2_500)
        options = await sel.locator("option").all_inner_texts()
        index = 1 if len(options) > 1 else 0
        await sel.select_option(index=index, timeout=2_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_select", label=label)
    return True


def _error_result(
    dealer: DealerConfig,
    observation_ts: datetime,
    error_message: str,
    state: _ScrapeState,
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
        scheduling_flow_seconds=state.scheduling_flow_seconds,
        interaction_steps=state.interaction_steps,
        platform=Platform.XTIME,
        source_payload_hash=state.source_payload_hash,
    )
