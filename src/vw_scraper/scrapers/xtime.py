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
    FrameLocator,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
)

from ..http import USER_AGENT, RobotsCache
from ..models import ScrapeResult, ScrapeStatus
from ..registry import DealerConfig, Platform

log = structlog.get_logger()

# Modern consumer.xtime.com SPA needs ~10s to render its start screen, plus
# ~30-40s to walk vehicle → service → transportation → appointment, plus a
# slot-XHR wait. TeamVelocity inline (West Islip) takes longer (~70-80s).
# Bumped 60→90→120 over the course of the rewrite as new pages were added.
# Orchestrator's _HARD_TIMEOUT_SECONDS in orchestrator.py tracks this with
# a 5s cushion.
DEALER_TIMEOUT_SECONDS = 120
NAVIGATION_TIMEOUT_MS = 30_000
VIEWPORT: ViewportSize = {"width": 1280, "height": 1800}

# Transportation step, in click-preference order. The modern consumer.xtime
# SPA (observed 2026-06) renders each option as a
# `<div class="panel-drop-off__slot" style="cursor:pointer">` row wrapping a
# `<div role="checkbox" aria-label="I'll wait at the dealership"|"I have a
# ride">`. The `.panel-drop-off__slot` ROW is the real click handler — same
# lesson as the service step: a click on the nested role="checkbox" div leaves
# the form invalid because the SPA's keyboard handler ignores synthesized
# clicks on it, so the slot-availability XHR never fires. (An earlier build
# used role="radio" on the option itself — kept as a legacy fallback.)
# TeamVelocity inline (West Islip) still uses a <label>-wrapped <input radio>.
# Any valid selection advances to the time step; we just need one to stick.
TRANSPORT_OPTION_SELECTORS: tuple[tuple[str, str], ...] = (
    ("transport wait slot (spa)", ".panel-drop-off__slot:has([role='checkbox'][aria-label*='wait' i])"),
    ("transport ride slot (spa)", ".panel-drop-off__slot:has([role='checkbox'][aria-label*='ride' i])"),
    ("transport first slot (spa)", ".panel-drop-off__slot"),
    ("transport wait checkbox (spa)", "[role='checkbox'][aria-label*='wait' i]"),
    ("transport wait radio (legacy)", "[role='radio'][aria-label*='wait' i]"),
    ("transport drop radio (legacy)", "[role='radio'][aria-label*='drop' i]"),
    ("transport wait label (tv)", "label:has-text('wait')"),
    ("transport ride label (tv)", "label:has-text('ride')"),
)
SLOT_BUDGET_SAFETY_SECONDS = 2.0
# Cap on response body bytes we'll try to JSON-decode. Xtime envelopes are
# <100KB in practice; we cap at 1MB to avoid burning CPU on oversize bundles.
MAX_BODY_BYTES_FOR_PARSE = 1_000_000

# Dummy registration data. SPEC.md line 144 permits dummy data where required
# to reach availability. Values are identifiably fake: example.com is reserved
# for docs/testing (RFC 2606); area code 555 + 555-01xx exchange is reserved
# for fictitious numbers (NANP). Miles is a plausible mid-life odometer.
DUMMY_FIRSTNAME = "Test"
DUMMY_LASTNAME = "User"
DUMMY_EMAIL = "test@example.com"
DUMMY_PHONE = "5555550100"
DUMMY_MILES = "50000"


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


def parse_slots_from_payload(
    payload: dict[str, Any], dealer_tz: str | None = None
) -> list[datetime]:
    """Extract slot datetimes from an Xtime XHR JSON envelope.

    Three envelope variants are handled:

    1. **Consumer Xtime SPA** (`x8con.xtime.com/xws/rest/dealer/.../appointment/getFirstAvailability`):
       `{"success": "True", "statusCode": "0", "availableTimes": {"Days": [...]}}`
       where each Day has `calendarDate` (`YYYY-MM-DD`) and `timeslots`
       (a list of `{"time": "HH:MM:SS"}`). Times are in dealer-local
       wall-clock — no tz in the payload. Caller passes `dealer_tz` (e.g.
       `"America/New_York"`) to localize; defaults to UTC if absent.

    2. **TeamVelocity Availabilities** (`xtime.teamvelocityportal.com/Xtime/Availabilities`):
       `{"success": true, "uiFormattedResponse": [{"calendarDate", "timeSlots": [<ISO>]}]}`
       Slot timestamps are already ISO-8601 with explicit offset.

    3. **TeamVelocity legacy / fixture tests** (`{"success": bool, "items": [...]}`):
       Each item has a timestamp under `startDateTime` / `appointmentDateTime` /
       similar key — ISO-8601 with offset.

    Returns slots in chronological order. Returns `[]` when the envelope
    is valid but reports no availability. Raises `XtimeParseError` when
    the envelope is malformed or the API itself reported an error.
    """
    if not isinstance(payload, dict):
        raise XtimeParseError(
            f"PARSE: expected dict envelope, got {type(payload).__name__}"
        )

    if "success" not in payload:
        raise XtimeParseError(
            f"PARSE: envelope missing 'success' key (got: {sorted(payload.keys())})"
        )

    # Xtime serializes booleans as the strings "True"/"False" in the
    # consumer.xtime.com responses, and as JSON true/false in the
    # TeamVelocity responses.
    success = payload["success"]
    if success not in (True, "True", "true"):
        msg = payload.get("message") or payload.get("errorMsgForEndUser") or "unknown"
        raise XtimeParseError(f"PARSE: Xtime API reported failure: {msg}")

    # Consumer.xtime.com variant.
    if "availableTimes" in payload:
        return _parse_consumer_available_times(payload["availableTimes"], dealer_tz)

    # TeamVelocity /Xtime/Availabilities variant.
    if "uiFormattedResponse" in payload:
        return _parse_team_velocity_availabilities(payload["uiFormattedResponse"])

    # TeamVelocity legacy items variant.
    if "items" not in payload:
        raise XtimeParseError(
            "PARSE: envelope has neither 'availableTimes', 'uiFormattedResponse', "
            f"nor 'items' (got: {sorted(payload.keys())})"
        )
    return _parse_team_velocity_items(payload["items"])


def _parse_team_velocity_availabilities(days: Any) -> list[datetime]:
    if not isinstance(days, list):
        raise XtimeParseError(
            f"PARSE: 'uiFormattedResponse' must be a list, got {type(days).__name__}"
        )
    slots: list[datetime] = []
    for day_index, day in enumerate(days):
        if not isinstance(day, dict):
            raise XtimeParseError(
                f"PARSE: uiFormattedResponse[{day_index}] is not a dict"
            )
        if day.get("isOpen") is False:
            continue
        time_slots = day.get("timeSlots")
        if time_slots is None:
            continue
        if not isinstance(time_slots, list):
            raise XtimeParseError(
                f"PARSE: uiFormattedResponse[{day_index}].timeSlots must be a list"
            )
        for slot_index, ts_str in enumerate(time_slots):
            if not isinstance(ts_str, str):
                raise XtimeParseError(
                    f"PARSE: uiFormattedResponse[{day_index}].timeSlots[{slot_index}] "
                    f"must be a string, got {type(ts_str).__name__}"
                )
            slots.append(_parse_iso_datetime(ts_str))
    slots.sort()
    return slots


def _parse_team_velocity_items(items: Any) -> list[datetime]:
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


def _parse_consumer_available_times(
    available_times: Any, dealer_tz: str | None
) -> list[datetime]:
    from zoneinfo import ZoneInfo

    if not isinstance(available_times, dict):
        raise XtimeParseError(
            f"PARSE: 'availableTimes' must be a dict, got {type(available_times).__name__}"
        )
    days = available_times.get("Days")
    if not isinstance(days, list):
        raise XtimeParseError(
            f"PARSE: 'availableTimes.Days' must be a list, "
            f"got {type(days).__name__}"
        )

    tz = ZoneInfo(dealer_tz) if dealer_tz else timezone.utc
    slots: list[datetime] = []
    for day_index, day in enumerate(days):
        if not isinstance(day, dict):
            raise XtimeParseError(
                f"PARSE: Days[{day_index}] is not a dict"
            )
        cal_date = day.get("calendarDate")
        timeslots = day.get("timeslots")
        if not cal_date or not timeslots:
            # Closed/full day — no slots is valid, just skip.
            continue
        if not isinstance(timeslots, list):
            raise XtimeParseError(
                f"PARSE: Days[{day_index}].timeslots must be a list"
            )
        for slot_index, slot in enumerate(timeslots):
            if not isinstance(slot, dict):
                raise XtimeParseError(
                    f"PARSE: Days[{day_index}].timeslots[{slot_index}] is not a dict"
                )
            time_str = slot.get("time")
            if not isinstance(time_str, str):
                raise XtimeParseError(
                    f"PARSE: Days[{day_index}].timeslots[{slot_index}] missing 'time' string"
                )
            iso = f"{cal_date}T{time_str}"
            try:
                naive = datetime.fromisoformat(iso)
            except ValueError as exc:
                raise XtimeParseError(
                    f"PARSE: invalid timestamp {iso!r}: {exc}"
                ) from exc
            slots.append(naive.replace(tzinfo=tz))
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


def _looks_like_json_xhr(response: Response) -> bool:
    """Cheap pre-filter: only attempt JSON decode on JSON-flavored XHR/fetch.

    Dealers proxy Xtime through their own domains (e.g. Teddy VW routes
    scheduling through `teddyvolkswagen.com/api/ServiceScheduler/*`), so
    host-based filtering drops the actual slot endpoint. Filter by resource
    type + content-type instead — broader but still keeps us from parsing
    HTML, images, or analytics beacons.
    """
    try:
        resource_type = response.request.resource_type
    except PlaywrightError:
        return False
    if resource_type not in ("xhr", "fetch"):
        return False
    content_type = response.headers.get("content-type", "").lower()
    return "json" in content_type


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
            # Apply playwright-stealth fingerprint patches. Teddy VW's
            # Vue3 inline scheduler (`prod.cdn.secureoffersites.com`)
            # detects headless Chromium and silently refuses to render
            # the form after consent. Stealth patches all common detector
            # hooks (webdriver flag, plugins, chrome.runtime, etc.).
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(context)
            page = await context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            # Future resolved by `_handle_response` when the first parseable
            # slot XHR arrives with ≥1 slot. Tuple is (slots, raw body bytes)
            # — we hash the raw bytes for `source_payload_hash`.
            slot_future: asyncio.Future[tuple[list[datetime], bytes]] = (
                asyncio.get_running_loop().create_future()
            )

            dealer_tz = dealer.config_json.get("timezone")

            def _on_response(response: Response) -> None:
                # Playwright's page.on is sync; hop into async to read body.
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

            # Cookie consent always lives on the outer dealer page, not inside
            # the embedded scheduler iframe. Run that step first against `page`.
            await _accept_cookie_consent(page, state, bound_log)
            # Same for "Already a customer?" sign-in reminder modals (West
            # Islip overlays one that intercepts pointer events on the form).
            await _dismiss_sign_in_modal(page, state, bound_log)

            # When the scheduler is embedded as an iframe (Jeff D'Ambrosio,
            # Teddy VW), all form interactions need to happen inside the frame.
            # When it's inline on the dealer page (West Islip team-velocity
            # variant), the scope stays as the page itself.
            scope = await _resolve_form_scope(page, dealer, bound_log)

            if dealer.config_json.get("vehicle_selection_required", True):
                await _walk_xtime_form(scope, state, bound_log)

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
    dealer_tz: str | None = None,
) -> None:
    """Parse any JSON XHR on the page; resolve `slot_future` on first slot hit.

    We don't know the slot endpoint URL ahead of time and it may live on the
    dealer's own domain (Teddy VW proxies through `/api/ServiceScheduler/*`),
    so we try every JSON XHR and let `parse_slots_from_payload`'s strict
    envelope check filter. The per-response work is bounded by response size
    and JSON-parsing speed, so this scales fine.
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
    if not isinstance(payload, dict):
        return
    try:
        slots = parse_slots_from_payload(payload, dealer_tz=dealer_tz)
    except XtimeParseError:
        return
    if slots and not slot_future.done():
        slot_future.set_result((slots, body_bytes))
        bound_log.info("slot_xhr_captured", url=response.url, slot_count=len(slots))


FormScope = Page | FrameLocator


async def _dismiss_sign_in_modal(
    page: Page, state: _ScrapeState, bound_log: Any
) -> None:
    """Close any "Already a customer?" sign-in reminder modal on the outer page.

    West Islip (TeamVelocity inline) shows a `#signInReminderModal` overlay
    with its own phone-input + Next button on top of the vehicle form. Until
    it's closed, every click on the underlying form gets intercepted with
    "subtree intercepts pointer events" from Playwright. Quick visibility
    probe so dealers without the modal don't pay any latency.
    """
    try:
        await page.locator("#signInReminderModal").wait_for(
            state="visible", timeout=1_500
        )
    except (PlaywrightTimeoutError, PlaywrightError):
        return
    for label, selector in (
        ("modal close button", "#signInReminderModal button.close"),
        ("modal x-close", "#signInReminderModal [aria-label*='close' i]"),
        ("modal generic close", ".modal.show button.close, .modal.in button.close"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(0.8)
            return


async def _accept_cookie_consent(page: Page, state: _ScrapeState, bound_log: Any) -> None:
    """Click through any cookie-consent banner on the outer dealer page.

    On Teddy VW (and other dealer.com sites) the third-party Xtime iframe is
    replaced with a `data:text/html` "Content Blocked pending your consent"
    stub until the user accepts. Accepting is a precondition for the
    scheduler iframe to load at all.

    Gated behind a quick visibility probe (1.5s total) — most dealers have
    no consent banner, and the unconditional 4-selector fallback used to
    burn ~10s of the dealer budget on those.
    """
    try:
        await page.locator(
            "button.ca-button-opt-in, button:has-text('Accept All'), button:has-text('I Agree')"
        ).first.wait_for(state="visible", timeout=1_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return
    for label, selector in (
        ("cookie allow", "button.ca-button-opt-in:has-text('Allow')"),
        ("cookie accept all", "button:has-text('Accept All')"),
        ("cookie agree", "button:has-text('I Agree')"),
        ("cookie deny", "button.ca-button-opt-in:has-text('Deny')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(1.5)
            return


async def _resolve_form_scope(
    page: Page,
    dealer: DealerConfig,
    bound_log: Any,
) -> FormScope:
    """Return the locator scope the walker should drive: page or iframe.

    Dealers split into two integration patterns:
      - **Inline** (West Islip / TeamVelocity variant): the year/make/model
        form is part of the dealer page DOM. `scope = page`.
      - **Iframe** (Jeff D'Ambrosio, Teddy VW): the form lives inside a
        `consumer.xtime.com` iframe and selectors on the outer page match
        unrelated marketing text. `scope = page.frame_locator(...)`.

    For iframe dealers, we wait for both the iframe element to attach AND
    for substantive SPA content to render inside it — Xtime's start screen
    typically appears 5–10s after the iframe attaches.
    """
    if not dealer.config_json.get("iframe_embedded"):
        return page

    src_pattern = dealer.config_json.get("iframe_src_pattern") or "consumer.xtime.com"
    iframe_selector = f"iframe[src*='{src_pattern}']"
    try:
        await page.locator(iframe_selector).first.wait_for(
            state="attached", timeout=10_000
        )
    except (PlaywrightTimeoutError, PlaywrightError):
        bound_log.warning(
            "iframe_not_attached",
            iframe_src_pattern=src_pattern,
            note="falling back to outer page; iframe may be consent-gated",
        )
        return page

    scope = page.frame_locator(iframe_selector)

    # Wait for the Xtime SPA to actually render its start screen. The modern
    # consumer.xtime.com SPA shows `#new_customer_button` (text:
    # "MAKE · YEAR · MODEL") within ~5–10s; older variants show a Next /
    # Get Started button. Any of these signals "content is interactive."
    ready_selectors = (
        "#new_customer_button",
        "button:has-text('MAKE')",
        "button:has-text('Get Started')",
        "button:has-text('Next')",
    )
    spa_ready = False
    for selector in ready_selectors:
        try:
            await scope.locator(selector).first.wait_for(state="visible", timeout=12_000)
            spa_ready = True
            bound_log.debug("xtime_spa_ready", matched=selector)
            break
        except (PlaywrightTimeoutError, PlaywrightError):
            continue

    if not spa_ready:
        bound_log.warning(
            "xtime_spa_not_ready",
            iframe_src_pattern=src_pattern,
            note="proceeding anyway; selectors may still match if content arrives mid-walk",
        )
    bound_log.info("entered_iframe", iframe_src_pattern=src_pattern, spa_ready=spa_ready)
    return scope


async def _walk_xtime_form(scope: FormScope, state: _ScrapeState, bound_log: Any) -> None:
    """Multi-phase walk of the Xtime flow within `scope` (page or frame).

    Phases:
      1. Splash — entry button ("Next" / "Get Started").
      2. Vehicle form — pick vehicle type, select year/make/model, fill
         miles + phone, submit.
      3. Service selection — click oil-change tile.
      4. Optional registration modal (some dealers gate here).

    Every step is best-effort: non-matching selectors no-op. The slot XHR
    may fire during any phase; `_handle_response` catches it regardless.

    The cookie-consent banner is handled separately (in `_accept_cookie_consent`)
    because it always lives on the outer dealer page, never inside the iframe.
    """
    # Phase 1: enter the appointment flow.
    # Modern xtime SPA (consumer.xtime.com): a "I'm a new customer" landing
    # page with `#new_customer_button` (text: "MAKE · YEAR · MODEL").
    # Legacy/inline variants use generic "Next" / "Get Started" buttons.
    for label, selector in (
        ("xtime spa new customer", "#new_customer_button"),
        ("splash next", "button:has-text('Next')"),
        ("splash get started", "button:has-text('Get Started')"),
        ("splash continue", "button:has-text('Continue')"),
        ("splash schedule service", "a:has-text('Schedule Service')"),
    ):
        if await _try_click(scope, selector, label, state, bound_log):
            await asyncio.sleep(2.5)
            break

    # Phase 2: vehicle picker. Modern xtime SPA uses a button-grid
    # (#MAKE-VOLKSWAGEN, #YEAR-2026, #MODEL-JETTA…); legacy uses cascading
    # <select>s. Try the button grid first, then fall back to selects.
    picked_grid = await _pick_vehicle_grid(scope, state, bound_log)
    if not picked_grid:
        # Legacy fallback: "Choose my car" radio + year/make/model selects.
        await _try_click(
            scope,
            "label:has-text('Choose my car')",
            "vehicle type: choose my car",
            state,
            bound_log,
        )
        await asyncio.sleep(0.8)
        await _pick_vehicle_triple(scope, state, bound_log)

    # Mileage — appears on both UI variants once vehicle is set. Phone fill
    # is intentionally gated to legacy variants only: on modern Xtime the
    # only "phone-looking" input on the vehicle page is the returning-
    # customer search on the start-screen overlay (hidden but still in DOM),
    # and filling it can transition the SPA off the new-customer flow.
    miles_filled = await _try_fill(
        scope,
        "input#miles, input[id*='mileage' i], input[placeholder*='Miles' i], input[name*='miles' i], input[name*='mileage' i]",
        DUMMY_MILES,
        "miles",
        state,
        bound_log,
    )
    if miles_filled:
        # Give the form's validation a beat to enable the continue button.
        await asyncio.sleep(1.5)

    # Phase 2b: submit / continue. Modern Xtime SPA uses `#continue_button`;
    # legacy TeamVelocity inline forms use an `<a id="scheduleservicenext">`
    # styled as a button; older variants use a `<button type="submit">`.
    # Playwright auto-waits for actionable state.
    if await _try_click_with_timeout(
        scope, "#continue_button", "vehicle continue (spa)", state, bound_log, timeout_ms=8_000
    ):
        await asyncio.sleep(3.0)
    else:
        for label, selector in (
            ("vehicle form next-a", "a#scheduleservicenext"),
            ("vehicle form submit", "button[type='submit']:has-text('Next')"),
            ("vehicle form next-button", "button:has-text('Next')"),
            ("vehicle form next-a-text", "a:has-text('Next')"),
        ):
            if await _try_click(scope, selector, label, state, bound_log):
                await asyncio.sleep(3.0)
                break

    # Phase 3: service selection. On a 2026 model Xtime's "recommended"
    # service list often *omits* oil change (algorithm assumes a new vehicle
    # doesn't need one yet), so we open the full catalog first.
    await _try_click(
        scope,
        "#all_services_repair_button",
        "expand all services",
        state,
        bound_log,
    )
    await asyncio.sleep(2.5)

    # The canonical Xtime catalog name is "Engine oil & filter - Change"
    # (per /xws/rest/services/.../unscheduledservices payload). Modern
    # Xtime renders each service with a hidden checkbox + a custom
    # checkbox div as the actual click target:
    #   <div class="service" role="listitem">
    #     <div class="service__row" aria-label="<service name>">...
    #       <div class="checkbox" role="checkbox" aria-label="<service name>"
    #            aria-checked="false">
    #         <label><input type="checkbox" name="service"></label>
    #       </div>
    #     </div>
    #   </div>
    # The role="checkbox" element is the right click target. Other
    # visually-clickable layers (`.service__select-service-area` /
    # `.service__expand-area`) either don't toggle selection or just
    # expand the detail panel.
    oil_aria_patterns = (
        "Engine oil",
        "Engine Oil",
        "Oil & Filter",
        "Oil and Filter",
        "Oil Change",
    )
    oil_selected = False
    for pattern in oil_aria_patterns:
        # `.service__row` is the actual click handler — empirically it
        # enables the continue button (form valid) while a click on the
        # nested `role="checkbox"` div leaves the form invalid (probably
        # because the SPA's keyboard-checkbox handler only fires on real
        # keyboard events, not synthesized clicks).
        row_selector = f".service__row[aria-label*='{pattern}']"
        if await _try_click(
            scope, row_selector, f"select service row: {pattern}", state, bound_log
        ):
            oil_selected = True
            await asyncio.sleep(2.0)
            break
    if not oil_selected:
        # TeamVelocity inline-form variant: services render as plain divs /
        # anchors / list items styled as tiles. Common-services page on
        # West Islip shows "Oil Change" alongside "Four Wheel Alignment",
        # "Tire Rotation", etc. as clickable cards.
        for label, selector in (
            ("oil change tile a", "a:has-text('Oil Change')"),
            ("oil change tile li", "li:has-text('Oil Change')"),
            ("oil change tile div class-service", "div[class*='service']:has-text('Oil Change')"),
            ("oil change tile div class-tile", "div[class*='tile']:has-text('Oil Change')"),
            ("oil change tile div class-card", "div[class*='card']:has-text('Oil Change')"),
            ("oil change button legacy", "button:has-text('Oil Change')"),
            ("oil change role-button", "[role='button']:has-text('Oil Change')"),
            ("oil filter button legacy", "button:has-text('oil & filter')"),
        ):
            if await _try_click(scope, selector, label, state, bound_log):
                await asyncio.sleep(2.0)
                break

    # Phase 3b: continue past services step.
    for label, selector in (
        ("post-service continue (spa)", "#continue_button"),
        ("post-service next-a", "a#scheduleservicenext"),
        ("post-service next-button", "button:has-text('Next')"),
        ("post-service continue", "button:has-text('Continue')"),
        ("post-service next-a-text", "a:has-text('Next')"),
    ):
        if await _try_click(scope, selector, label, state, bound_log):
            await asyncio.sleep(2.5)
            break

    # Phase 3c: transportation step. Two variants:
    #   - Consumer.xtime SPA: `<div role="radio" aria-label="<option>">`
    #   - TeamVelocity inline (West Islip): `<input type="radio" name="ride type">`
    #     wrapped in a `<label>` whose text identifies the option.
    for label, selector in TRANSPORT_OPTION_SELECTORS:
        if await _try_click(scope, selector, label, state, bound_log):
            await asyncio.sleep(1.5)
            break
    for label, selector in (
        ("post-transport continue (spa)", "#continue_button"),
        ("post-transport next-a", "a#scheduleservicenext"),
        ("post-transport next-button", "button:has-text('Next')"),
        ("post-transport next-a-text", "a:has-text('Next')"),
    ):
        if await _try_click(scope, selector, label, state, bound_log):
            await asyncio.sleep(3.0)
            break

    # Phase 4: optional registration modal (dealer-dependent).
    # Probe-style detection — only spend time filling if a registration field
    # is actually visible. Burning the 60s budget waiting for fields that
    # never appear blocks the slot XHR from being captured.
    try:
        first_loc = scope.locator(
            "input[name*='first' i], input[placeholder*='first' i], input#firstName"
        ).first
        await first_loc.wait_for(state="visible", timeout=1_500)
        registration_visible = True
    except (PlaywrightTimeoutError, PlaywrightError):
        registration_visible = False

    if registration_visible:
        for label, selector, value in (
            ("firstname", "input[name*='first' i], input[placeholder*='first' i], input#firstName", DUMMY_FIRSTNAME),
            ("lastname", "input[name*='last' i], input[placeholder*='last' i], input#lastName", DUMMY_LASTNAME),
            ("email", "input[type='email'], input[name*='email' i], input#email", DUMMY_EMAIL),
        ):
            await _try_fill(scope, selector, value, label, state, bound_log)
        bound_log.info("registration_modal_filled_with_dummy_data")
        for label, selector in (
            ("registration continue", "button:has-text('Continue')"),
            ("registration next", "button:has-text('Next')"),
            ("registration submit", "button:has-text('Submit')"),
        ):
            if await _try_click(scope, selector, label, state, bound_log):
                await asyncio.sleep(2.0)
                break


async def _pick_vehicle_grid(
    scope: FormScope,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    """Pick MAKE → YEAR → MODEL on the modern xtime SPA's button grid.

    Returns True if the grid was found and at least make+year+model were
    clicked. Returns False if the page isn't using the button grid (caller
    should fall back to the legacy select-based picker).
    """
    # Detect the grid by looking for a MAKE button (modern SPA uses ids like
    # `MAKE-VOLKSWAGEN`, `MAKE-TOYOTA`, `MAKE-OTHER`).
    try:
        make_locator = scope.locator("button[id^='MAKE-']").first
        await make_locator.wait_for(state="visible", timeout=4_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False

    # Prefer VOLKSWAGEN; fall back to the first non-OTHER button.
    clicked_make = False
    if await _try_click(scope, "button#MAKE-VOLKSWAGEN", "make: volkswagen", state, bound_log):
        clicked_make = True
    else:
        if await _try_click(
            scope,
            "button[id^='MAKE-']:not([id='MAKE-OTHER'])",
            "make: first non-other",
            state,
            bound_log,
        ):
            clicked_make = True
    if not clicked_make:
        return False
    await asyncio.sleep(1.0)

    # Pick the most recent year (first YEAR button visible) — older years are
    # listed first in DOM in some layouts and last in others; preferring the
    # newest is a reasonable heuristic for a "currently-owned" plausible
    # vehicle. Probes show YEAR-2026 / YEAR-2025 are common.
    year_clicked = False
    for selector in (
        "button#YEAR-2026",
        "button#YEAR-2025",
        "button#YEAR-2024",
        "button[id^='YEAR-']:not([disabled])",
    ):
        if await _try_click(scope, selector, f"year: {selector}", state, bound_log):
            year_clicked = True
            break
    if not year_clicked:
        bound_log.debug("vehicle_grid_no_year_button")
        return clicked_make  # still partial progress
    await asyncio.sleep(1.0)

    # Pick the first model button.
    await _try_click(
        scope,
        "button[id^='MODEL-']:not([disabled])",
        "model: first",
        state,
        bound_log,
    )
    await asyncio.sleep(1.0)
    return True


async def _try_click(
    scope: FormScope,
    selector: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    return await _try_click_with_timeout(scope, selector, label, state, bound_log, 2_500)


async def _try_click_with_timeout(
    scope: FormScope,
    selector: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
    timeout_ms: int,
) -> bool:
    try:
        locator = scope.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        try:
            await locator.scroll_into_view_if_needed(timeout=2_000)
        except (PlaywrightTimeoutError, PlaywrightError):
            pass
        try:
            await locator.click(timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # The Xtime iframe footer (continue_button) sits below the fold
            # and the iframe owns its own scroller; Playwright's actionability
            # check reports "element is outside of the viewport" even after
            # explicit scroll. `dispatch_event('click')` bypasses the check
            # by firing the click event directly on the element.
            await locator.dispatch_event("click", timeout=timeout_ms)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_click", label=label)
    return True


async def _try_fill(
    scope: FormScope,
    selector: str,
    value: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    try:
        locator = scope.locator(selector).first
        await locator.wait_for(state="visible", timeout=2_000)
        await locator.fill(value, timeout=2_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False
    state.interaction_steps += 1
    bound_log.debug("step_fill", label=label)
    return True


async def _try_select_first_real_option(
    scope: FormScope,
    selector: str,
    label: str,
    state: _ScrapeState,
    bound_log: Any,
) -> bool:
    """Select the first real <option> (skipping a 'Select…' placeholder).

    Cascading dropdowns (year → make → model on Xtime) populate their
    downstream options via XHR. We poll for a real option to appear before
    trying to select — otherwise select_option picks the placeholder and
    downstream validation fails silently on form submit.
    """
    try:
        sel = scope.locator(selector).first
        await sel.wait_for(state="visible", timeout=2_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False

    real_value = await _wait_for_select_real_option(sel, timeout_s=6.0)
    if real_value is None:
        bound_log.debug("step_select_empty", label=label)
        return False

    try:
        await sel.select_option(value=real_value, timeout=2_500)
    except (PlaywrightTimeoutError, PlaywrightError):
        return False

    state.interaction_steps += 1
    bound_log.debug("step_select", label=label, value=real_value)
    return True


async def _pick_vehicle_triple(scope: FormScope, state: _ScrapeState, bound_log: Any) -> None:
    """Pick year → make → model. Skip year/make combinations that yield only 'OTHER'.

    Cascading dropdowns on Xtime mean make depends on year, model depends on
    make. We iterate recent years, preferring one whose make list contains a
    real (non-'OTHER') option — typically 'VOLKSWAGEN' on a VW dealer. Falls
    back to any first-real-option combination if nothing matches.
    """
    try:
        year_sel = scope.locator("select#year, select[name*='year' i]").first
        await year_sel.wait_for(state="visible", timeout=3_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        return

    try:
        year_values_raw = await year_sel.locator("option").evaluate_all(
            "(nodes) => nodes.map(n => n.value)"
        )
    except PlaywrightError:
        return
    year_values: list[str] = [v for v in year_values_raw if isinstance(v, str) and v]
    if not year_values:
        return

    for year_value in year_values[:5]:
        try:
            await year_sel.select_option(value=year_value, timeout=2_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
        await asyncio.sleep(1.2)

        try:
            make_sel = scope.locator("select#make, select[name*='make' i]").first
            await make_sel.wait_for(state="visible", timeout=2_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            continue

        real_makes = await _wait_for_non_other_option(make_sel, timeout_s=5.0)
        if not real_makes:
            bound_log.debug("year_rejected_no_real_make", year=year_value)
            continue

        make_value = "VOLKSWAGEN" if "VOLKSWAGEN" in real_makes else real_makes[0]
        try:
            await make_sel.select_option(value=make_value, timeout=2_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
        state.interaction_steps += 2  # year + make
        bound_log.debug("step_select", label="year", value=year_value)
        bound_log.debug("step_select", label="make", value=make_value)
        await asyncio.sleep(1.2)

        try:
            model_sel = scope.locator("select#model, select[name*='model' i]").first
            await model_sel.wait_for(state="visible", timeout=2_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            return  # year+make picked; skip model and let form submit handle it

        model_value = await _wait_for_select_real_option(model_sel, timeout_s=5.0)
        if model_value is None:
            return
        try:
            await model_sel.select_option(value=model_value, timeout=2_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            return
        state.interaction_steps += 1
        bound_log.debug("step_select", label="model", value=model_value)
        return


async def _wait_for_non_other_option(sel: Any, timeout_s: float) -> list[str]:
    """Poll for <option> values that are real and not the 'OTHER' placeholder.

    Xtime returns 'OTHER' as a catch-all when a vendor-year combination has
    no catalogued models — picking it reliably breaks downstream scheduling.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            values = await sel.locator("option").evaluate_all(
                "(nodes) => nodes.map(n => n.value)"
            )
        except PlaywrightError:
            values = []
        real = [v for v in values if isinstance(v, str) and v and v != "OTHER"]
        if real:
            return real
        await asyncio.sleep(0.25)
    return []


async def _wait_for_select_real_option(sel: Any, timeout_s: float) -> str | None:
    """Poll the <select> for a non-empty option value. Returns the value or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            values = await sel.locator("option").evaluate_all(
                "(nodes) => nodes.map(n => n.value)"
            )
        except PlaywrightError:
            values = []
        for v in values:
            if isinstance(v, str) and v:
                return v
        await asyncio.sleep(0.25)
    return None


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
