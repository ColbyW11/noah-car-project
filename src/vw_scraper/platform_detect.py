"""Detect which scheduling platform a VW dealer uses.

Pure-HTML detection so it can be tested in milliseconds against fixtures
(CLAUDE.md: fixture-based parser tests). The async `detect_platform(page)`
wrapper collects signals from a live Playwright page and delegates.

Signatures live as module-level constants — Slice 8 adds new platforms by
appending rows, not by editing detection logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .registry import Platform

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass(frozen=True)
class PlatformSignature:
    """A set of substrings that, if any match, indicate the platform."""

    platform: Platform
    url_substrings: tuple[str, ...] = ()
    html_substrings: tuple[str, ...] = ()


# Order matters: first match wins. URL/iframe matches are most reliable,
# followed by script src URLs embedded in the page HTML, then DOM markers.
PLATFORM_SIGNATURES: tuple[PlatformSignature, ...] = (
    PlatformSignature(
        platform=Platform.XTIME,
        url_substrings=(
            "xtime.com",
            "xtimeappointment.com",
        ),
        html_substrings=(
            "xtime.com",
            "xtimeappointment",
            "consumerschedulingfe",
        ),
    ),
    PlatformSignature(
        platform=Platform.MYKAARMA,
        url_substrings=(
            "mykaarma.com",
        ),
        html_substrings=(
            "mykaarma.com",
            "mk-scheduler",
            "mykaarma-widget",
        ),
    ),
    PlatformSignature(
        platform=Platform.DEALER_FX,
        url_substrings=(
            "dealer-fx.com",
            "dealerfx.com",
        ),
        html_substrings=(
            "dealer-fx.com",
            "dealerfx.com",
            "dfx-widget",
        ),
    ),
    PlatformSignature(
        platform=Platform.CONNECT_CDK,
        url_substrings=(
            "connectcdk.com",
        ),
        html_substrings=(
            "connectcdk.com",
            "nc-cosa-consumer-ui",
        ),
    ),
)

# If no vendor signature matches but the page looks like it has its own
# scheduler (date/time picker hosted on the dealer's own domain), classify as
# custom rather than unknown.
CUSTOM_SCHEDULER_MARKERS: tuple[str, ...] = (
    'type="date"',
    "date-picker",
    "datepicker",
    "time-slot",
    "timeslot",
    "appointment-time",
)


def detect_platform_from_signals(
    page_html: str,
    page_url: str,
    iframe_srcs: list[str],
) -> Platform:
    """Classify the platform from static signals.

    Pure function — no I/O, no Playwright. Keep it this way so fixture tests
    can drive it directly.
    """
    html_lower = page_html.lower()
    url_lower = page_url.lower()
    iframe_lower = [src.lower() for src in iframe_srcs]

    for sig in PLATFORM_SIGNATURES:
        for needle in sig.url_substrings:
            if needle in url_lower:
                return sig.platform
            if any(needle in src for src in iframe_lower):
                return sig.platform
        for needle in sig.html_substrings:
            if needle in html_lower:
                return sig.platform

    for marker in CUSTOM_SCHEDULER_MARKERS:
        if marker in html_lower:
            return Platform.CUSTOM

    return Platform.UNKNOWN


async def detect_platform(page: Page) -> Platform:
    """Classify the platform of the currently-loaded page.

    Collects HTML, final URL, and iframe srcs (Xtime/Dealer-FX often embed as
    iframes on a vendor subdomain), then delegates to the pure helper.
    """
    html = await page.content()
    iframe_srcs = [frame.url for frame in page.frames if frame.url]
    return detect_platform_from_signals(html, page.url, iframe_srcs)
