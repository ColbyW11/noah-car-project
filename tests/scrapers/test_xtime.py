"""Tests for vw_scraper.scrapers.xtime — fixture-based parser tests.

Scenarios from SLICES.md Slice 3: slots available, no slots, malformed HTML,
login wall. Live navigation is Slice 4 (`@pytest.mark.live`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vw_scraper.models import ScrapeStatus
from vw_scraper.registry import Platform, load_registry
from vw_scraper.scrapers.base import PlatformScraper
from vw_scraper.scrapers.xtime import (
    XtimeParseError,
    XtimeScraper,
    detect_login_wall,
    parse_slots_from_payload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scrapers" / "xtime"


def _load_json(rel_path: str) -> dict:
    return json.loads((FIXTURES / rel_path).read_text())


def _load_html(rel_path: str) -> str:
    return (FIXTURES / rel_path).read_text()


def test_xtime_parses_slots_when_availability_exists() -> None:
    payload = _load_json("slots_available/xhr_response.json")

    slots = parse_slots_from_payload(payload)

    assert len(slots) == 5
    assert all(isinstance(s, datetime) for s in slots)
    assert all(s.tzinfo is not None for s in slots), "slots must be timezone-aware"
    assert slots == sorted(slots), "slots must be returned in chronological order"
    assert slots[0] == datetime.fromisoformat("2026-04-20T09:00:00-04:00")


def test_xtime_returns_empty_list_when_no_availability() -> None:
    payload = _load_json("no_slots_available/xhr_response.json")

    slots = parse_slots_from_payload(payload)

    assert slots == []


def test_xtime_raises_parse_error_on_envelope_failure() -> None:
    payload = {
        "success": False,
        "code": 500,
        "message": "Internal Server Error",
        "items": [],
        "errorMsgForEndUser": ["something went wrong"],
    }

    with pytest.raises(XtimeParseError) as exc:
        parse_slots_from_payload(payload)

    assert "PARSE:" in str(exc.value)


def test_xtime_raises_parse_error_on_missing_envelope_keys() -> None:
    with pytest.raises(XtimeParseError) as exc:
        parse_slots_from_payload({"unrelated": "shape"})

    assert "PARSE:" in str(exc.value)
    assert "items" in str(exc.value) or "success" in str(exc.value)


def test_xtime_raises_parse_error_on_malformed_html_input() -> None:
    """Malformed HTML must not be passed to the JSON parser; if it is, fail loudly."""
    html = _load_html("malformed_html/schedule_page.html")

    with pytest.raises(XtimeParseError):
        parse_slots_from_payload(html)  # type: ignore[arg-type]


def test_xtime_detects_login_wall_in_html() -> None:
    html = _load_html("login_wall/schedule_page.html")
    assert detect_login_wall(html) is True


def test_xtime_does_not_falsely_flag_login_wall_on_normal_page() -> None:
    html = _load_html("slots_available/schedule_page.html")
    # Real Xtime page may mention "sign in" optionally, but the registration
    # modal is *not* the same as a hard login wall — assert we don't flag it.
    # If this assertion fails it tells us our marker list is too aggressive.
    assert detect_login_wall(html) is False


def test_xtime_handles_trailing_z_utc_timestamps() -> None:
    payload = {
        "success": True,
        "code": None,
        "message": "Success",
        "items": [{"startDateTime": "2026-04-20T09:00:00Z"}],
        "errorMsgForEndUser": [],
    }
    slots = parse_slots_from_payload(payload)
    assert slots == [datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)]


def test_xtime_scraper_satisfies_protocol() -> None:
    scraper = XtimeScraper()
    assert isinstance(scraper, PlatformScraper)
    assert scraper.platform_name == Platform.XTIME.value


@pytest.mark.live
@pytest.mark.asyncio
async def test_xtime_scrape_vw0001_live() -> None:
    """Live end-to-end scrape of VW0001. Skipped unless `pytest -m live`.

    Accepts either a successful result with ≥1 slot or a loud error with a
    recognized prefix — CLAUDE.md error-handling convention. "The site has no
    availability right now" is a valid outcome that shouldn't fail the test.
    """
    from playwright.async_api import async_playwright

    dealers = {d.dealer_code: d for d in load_registry(REGISTRY_CSV)}
    dealer = dealers["VW0001"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            result = await XtimeScraper().scrape(dealer, browser)
        finally:
            await browser.close()

    assert result.dealer_code == "VW0001"
    assert result.platform is Platform.XTIME
    assert result.scraper_version  # populated from package __version__

    if result.scrape_status is ScrapeStatus.SUCCESS:
        assert result.slot_count >= 1
        assert result.first_available_ts is not None
        assert result.first_available_ts.tzinfo is not None
        assert result.scheduling_flow_seconds is not None
        assert result.scheduling_flow_seconds > 0
        assert result.lead_time_hours is not None
        assert result.source_payload_hash is not None
        assert result.source_payload_hash.startswith("sha256:")
    else:
        assert result.scrape_status is ScrapeStatus.ERROR
        assert result.error_message is not None
        assert result.error_message.split(":", 1)[0] in {
            "TIMEOUT",
            "PARSE",
            "NAVIGATION",
            "UNEXPECTED",
        }
