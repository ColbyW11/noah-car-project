"""Xtime scraper.

Slice 3 implements the pure parsing functions only — `XtimeScraper.scrape()`
is stubbed and will be wired to a live browser session in Slice 4.

Xtime widgets render inside the dealer page (dealer.com / Vue) but load all
slot data from `xtime.teamvelocityportal.com` via XHR using a consistent
envelope: `{success, code, message, items, errorMsgForEndUser}`. The slot
items shape is best-guess pending Slice 4 live validation; the parser is
written to handle a few common field-name variants so the live wire-up has
the smallest possible delta.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from playwright.async_api import Browser

from ..models import ScrapeResult
from ..registry import DealerConfig, Platform


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


class XtimeScraper:
    """PlatformScraper for the Xtime / TeamVelocity oil-change widget.

    Slice 3 builds only the parser. `scrape()` is wired to a live browser
    session in Slice 4 (see SLICES.md).
    """

    platform_name: str = Platform.XTIME.value

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult:
        raise NotImplementedError("XtimeScraper.scrape() is wired in Slice 4")
