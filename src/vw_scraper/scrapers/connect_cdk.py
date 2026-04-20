"""ConnectCDK scraper (parser + router stub; live wiring in a follow-up slice).

VW0005 (vwnanuet.com) loads the ConnectCDK / VW SHIFT scheduler in an iframe
served from `api.connectcdk.com`. The iframe's React app talks to the CDK
microservice host `nc-cdk-service-cosa-microservice.na.connectcdk.com` for
dealer config and (eventually) availability data.

Unlike Xtime's `{success, code, message, items}` envelope, ConnectCDK tends
to return raw top-level JSON — captured endpoints like `/Teams` and
`/GetDealerFeatureSettings` return lists directly; `/DealerInfo`, `/Settings`
return plain objects. The parser below accepts either form: a bare list of
slot dicts, or a dict with a slot-list under `slots` / `availableSlots` /
`appointments` / `items`. The canonical slot-endpoint URL is not yet pinned
down — the capture script (`scripts/capture_connect_cdk_fixtures.py`) only
reached the landing page ("RETURNING CUSTOMER / NEW CUSTOMER") because
walking past that requires simulating a customer flow we don't yet wire.
Live navigation lands in a follow-up slice; until then, `scrape()` returns
a loud structured error so VW0005 produces a clear error observation rather
than silently succeeding with empty slots.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from playwright.async_api import Browser

from ..models import ScrapeResult, ScrapeStatus
from ..registry import DealerConfig, Platform

log = structlog.get_logger()


class ConnectCdkParseError(Exception):
    """Raised when a ConnectCDK availability payload cannot be parsed.

    Caught at the scraper boundary and turned into a `ScrapeResult` with
    `scrape_status='error'` and a `PARSE:` error_message prefix
    (CLAUDE.md error-handling convention).
    """


_SLOT_LIST_KEYS = ("availableSlots", "slots", "appointments", "items")
_SLOT_TIME_KEYS = (
    "startDateTime",
    "appointmentDateTime",
    "slotStart",
    "startTime",
    "dateTime",
)

_LOGIN_WALL_MARKERS = (
    "please sign in to continue",
    "enter the code we sent",
    "temporary access code",
    "otp verification required",
    "you must sign in",
)

_TRAILING_Z = re.compile(r"Z$")


def parse_slots_from_payload(payload: Any) -> list[datetime]:
    """Extract slot datetimes from a ConnectCDK availability payload.

    Accepts a bare list of slot dicts or a dict with the slot list under
    one of `availableSlots` / `slots` / `appointments` / `items`. Returns
    slots in chronological order; `[]` when the payload is well-formed but
    reports no availability. Raises `ConnectCdkParseError` on malformed
    payloads or when a slot item lacks a recognizable timestamp field.
    """
    items = _extract_slot_list(payload)
    slots: list[datetime] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConnectCdkParseError(
                f"PARSE: items[{index}] is not a dict (got {type(item).__name__})"
            )
        ts_str = _first_present_string(item, _SLOT_TIME_KEYS)
        if ts_str is None:
            raise ConnectCdkParseError(
                f"PARSE: items[{index}] has none of {_SLOT_TIME_KEYS}; "
                f"keys present: {sorted(item.keys())}"
            )
        slots.append(_parse_iso_datetime(ts_str))
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


def _parse_iso_datetime(value: str) -> datetime:
    normalized = _TRAILING_Z.sub("+00:00", value)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConnectCdkParseError(
            f"PARSE: invalid ISO-8601 timestamp {value!r}"
        ) from exc


class ConnectCdkScraper:
    """PlatformScraper for the ConnectCDK / VW SHIFT scheduler.

    Current scope (Slice 8): parser functions + router registration only.
    `scrape()` is a structured-error stub — live iframe navigation lands in
    a follow-up slice. Until then VW0005 produces a loud, prefixed error
    observation rather than silently reporting no availability (SPEC.md
    principle #6: loud failure, never silent drift).
    """

    platform_name: str = Platform.CONNECT_CDK.value

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult:
        observation_ts = datetime.now(timezone.utc)
        log.bind(dealer_code=dealer.dealer_code).warning(
            "connect_cdk_scrape_not_implemented"
        )
        return ScrapeResult(
            dealer_code=dealer.dealer_code,
            observation_ts=observation_ts,
            scrape_status=ScrapeStatus.ERROR,
            error_message="UNEXPECTED: live scrape not yet implemented for connect_cdk",
            first_available_ts=None,
            lead_time_hours=None,
            available_slots=[],
            slot_count=0,
            scheduling_flow_seconds=None,
            interaction_steps=0,
            platform=Platform.CONNECT_CDK,
            source_payload_hash=None,
        )
