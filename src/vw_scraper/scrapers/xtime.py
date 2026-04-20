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
        slots = parse_slots_from_payload(payload)
    except XtimeParseError:
        return
    if slots and not slot_future.done():
        slot_future.set_result((slots, body_bytes))
        bound_log.info("slot_xhr_captured", url=response.url, slot_count=len(slots))


async def _walk_xtime_form(page: Page, state: _ScrapeState, bound_log: Any) -> None:
    """Multi-phase walk of the Xtime flow.

    Shape derived from VW0001 (Teddy VW), which is representative of the
    dealer.com + Xtime embed pattern seen on other dealers:
      1. Splash — dismiss cookie banner, click "Next" to enter.
      2. Vehicle form — pick "Choose my car" vehicle-type radio, select
         year/make/model, fill miles + phone, submit.
      3. Service selection — click oil-change tile.
      4. Optional registration modal (some dealers gate here, not VW0001).

    Every step is best-effort: non-matching selectors no-op. The slot XHR
    may fire during any phase; `_handle_response` catches it regardless.
    """
    # Phase 1: dismiss cookie consent banner if present, then enter the flow.
    for label, selector in (
        ("cookie allow", "button.ca-button-opt-in:has-text('Allow')"),
        ("cookie deny", "button.ca-button-opt-in:has-text('Deny')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            break

    for label, selector in (
        ("splash next", "button:has-text('Next')"),
        ("splash get started", "button:has-text('Get Started')"),
        ("splash continue", "button:has-text('Continue')"),
        ("splash schedule service", "a:has-text('Schedule Service')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(2.0)
            break

    # Phase 2: vehicle form.
    # "Choose my car" radio makes year/make/model selects active on Teddy VW.
    await _try_click(
        page,
        "label:has-text('Choose my car')",
        "vehicle type: choose my car",
        state,
        bound_log,
    )
    await asyncio.sleep(0.8)

    # Pick a year/make/model triple where the make list has a real VW option.
    # Xtime's make list is populated by year (cascading XHR), and "future" years
    # sometimes return only 'OTHER' placeholder data. We walk years until we
    # get a non-placeholder make — preferring Volkswagen on a VW dealer.
    await _pick_vehicle_triple(page, state, bound_log)

    await _try_fill(
        page,
        "input#miles, input[placeholder*='Miles' i], input[name*='miles' i]",
        DUMMY_MILES,
        "miles",
        state,
        bound_log,
    )
    await _try_fill(
        page,
        "input#phone, input[type='tel'], input[name*='phone' i]",
        DUMMY_PHONE,
        "phone",
        state,
        bound_log,
    )

    for label, selector in (
        ("vehicle form submit", "button[type='submit']:has-text('Next')"),
        ("vehicle form next (fallback)", "button:has-text('Next')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(2.5)
            break

    # Phase 3: service selection.
    for label, selector in (
        ("oil change tile", "*:has-text('Oil Change')"),
        ("oil + filter tile", "*:has-text('Oil & Filter')"),
        ("oil filter tile", "*:has-text('Oil and Filter')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(2.0)
            break

    # Phase 3b: some dealers want a "Next" / "Continue" after picking service.
    for label, selector in (
        ("post-service next", "button:has-text('Next')"),
        ("post-service continue", "button:has-text('Continue')"),
    ):
        if await _try_click(page, selector, label, state, bound_log):
            await asyncio.sleep(2.0)
            break

    # Phase 4: optional registration modal (dealer-dependent).
    filled_any = False
    for label, selector, value in (
        ("firstname", "input[name*='first' i], input[placeholder*='first' i], input#firstName", DUMMY_FIRSTNAME),
        ("lastname", "input[name*='last' i], input[placeholder*='last' i], input#lastName", DUMMY_LASTNAME),
        ("email", "input[type='email'], input[name*='email' i], input#email", DUMMY_EMAIL),
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
                await asyncio.sleep(2.0)
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
    """Select the first real <option> (skipping a 'Select…' placeholder).

    Cascading dropdowns (year → make → model on Xtime) populate their
    downstream options via XHR. We poll for a real option to appear before
    trying to select — otherwise select_option picks the placeholder and
    downstream validation fails silently on form submit.
    """
    try:
        sel = page.locator(selector).first
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


async def _pick_vehicle_triple(page: Page, state: _ScrapeState, bound_log: Any) -> None:
    """Pick year → make → model. Skip year/make combinations that yield only 'OTHER'.

    Cascading dropdowns on Xtime mean make depends on year, model depends on
    make. We iterate recent years, preferring one whose make list contains a
    real (non-'OTHER') option — typically 'VOLKSWAGEN' on a VW dealer. Falls
    back to any first-real-option combination if nothing matches.
    """
    try:
        year_sel = page.locator("select#year, select[name*='year' i]").first
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
            make_sel = page.locator("select#make, select[name*='make' i]").first
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
            model_sel = page.locator("select#model, select[name*='model' i]").first
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
