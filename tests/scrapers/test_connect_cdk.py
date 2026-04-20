"""Tests for vw_scraper.scrapers.connect_cdk — fixture-based parser tests.

Scope per Slice 8: parser + router + fixtures only. Live iframe navigation
lands in a follow-up slice; until then `ConnectCdkScraper.scrape()` is a
structured-error stub whose output this suite asserts against so we don't
silently drift past the stub.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vw_scraper.models import ScrapeStatus
from vw_scraper.registry import DealerConfig, Platform
from vw_scraper.scrapers import get_scraper, registered_platforms
from vw_scraper.scrapers.base import PlatformScraper
from vw_scraper.scrapers.connect_cdk import (
    ConnectCdkParseError,
    ConnectCdkScraper,
    detect_login_wall,
    parse_slots_from_payload,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scrapers" / "connect_cdk"


def _load_json(rel_path: str) -> dict:
    return json.loads((FIXTURES / rel_path).read_text())


def _load_html(rel_path: str) -> str:
    return (FIXTURES / rel_path).read_text()


def test_connect_cdk_parses_slots_when_availability_exists() -> None:
    payload = _load_json("slots_available/xhr_response.json")

    slots = parse_slots_from_payload(payload)

    assert len(slots) == 4
    assert all(isinstance(s, datetime) for s in slots)
    assert all(s.tzinfo is not None for s in slots), "slots must be timezone-aware"
    assert slots == sorted(slots), "slots must be returned in chronological order"
    assert slots[0] == datetime.fromisoformat("2026-04-21T08:00:00-04:00")


def test_connect_cdk_returns_empty_list_when_no_availability() -> None:
    payload = _load_json("no_slots_available/xhr_response.json")

    slots = parse_slots_from_payload(payload)

    assert slots == []


def test_connect_cdk_accepts_bare_list_payload() -> None:
    """CDK microservice endpoints like /Teams return raw lists — the parser
    must accept that shape too, not only wrapper dicts."""
    payload = [
        {"startDateTime": "2026-04-21T08:00:00-04:00", "isAvailable": True},
        {"startDateTime": "2026-04-21T10:30:00-04:00", "isAvailable": True},
    ]

    slots = parse_slots_from_payload(payload)

    assert len(slots) == 2
    assert slots[0] == datetime.fromisoformat("2026-04-21T08:00:00-04:00")


def test_connect_cdk_raises_parse_error_on_malformed_payload() -> None:
    payload = _load_json("malformed_payload/xhr_response.json")

    with pytest.raises(ConnectCdkParseError) as exc:
        parse_slots_from_payload(payload)

    assert "PARSE:" in str(exc.value)


def test_connect_cdk_raises_parse_error_on_non_dict_non_list_payload() -> None:
    with pytest.raises(ConnectCdkParseError) as exc:
        parse_slots_from_payload("not json")  # type: ignore[arg-type]

    assert "PARSE:" in str(exc.value)
    assert "str" in str(exc.value)


def test_connect_cdk_raises_parse_error_on_slot_missing_timestamp_field() -> None:
    payload = {"availableSlots": [{"isAvailable": True, "displayTime": "8:00 AM"}]}

    with pytest.raises(ConnectCdkParseError) as exc:
        parse_slots_from_payload(payload)

    assert "PARSE:" in str(exc.value)


def test_connect_cdk_handles_trailing_z_utc_timestamps() -> None:
    payload = {
        "availableSlots": [{"startDateTime": "2026-04-21T08:00:00Z"}],
    }
    slots = parse_slots_from_payload(payload)
    assert slots == [datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc)]


def test_connect_cdk_detects_login_wall_in_html() -> None:
    html = _load_html("login_wall/iframe_page.html")
    assert detect_login_wall(html) is True


def test_connect_cdk_does_not_falsely_flag_login_wall_on_landing_page() -> None:
    """The real iframe landing screen ("RETURNING CUSTOMER / NEW CUSTOMER")
    isn't a login wall — it's a customer-identification step. If this fails
    the marker list has grown too aggressive."""
    html = _load_html("slots_available/iframe_page.html")
    assert detect_login_wall(html) is False


def test_connect_cdk_scraper_satisfies_protocol() -> None:
    scraper = ConnectCdkScraper()
    assert isinstance(scraper, PlatformScraper)
    assert scraper.platform_name == Platform.CONNECT_CDK.value


@pytest.mark.asyncio
async def test_connect_cdk_scrape_returns_stub_error_until_live_wiring_lands() -> None:
    """`scrape()` is a structured-error stub in Slice 8. The error message is
    asserted here so the stub can't silently turn into a no-op; the
    follow-up live-wiring slice is expected to delete this test."""
    scraper = ConnectCdkScraper()
    dealer = DealerConfig(
        dealer_code="VW0005",
        dealer_name="Vwnanuet",
        dealer_url="https://vwnanuet.com",
        schedule_url="https://www.vwnanuet.com/schedule-service.htm",
        platform=Platform.CONNECT_CDK,
        zip="",
        region="",
        config_json={},
        active=True,
        notes="",
    )

    result = await scraper.scrape(dealer, AsyncMock())

    assert result.scrape_status is ScrapeStatus.ERROR
    assert result.error_message is not None
    assert result.error_message.startswith("UNEXPECTED:")
    assert "connect_cdk" in result.error_message
    assert result.platform is Platform.CONNECT_CDK
    assert result.slot_count == 0
    assert result.available_slots == []


def test_router_returns_scraper_for_registered_platforms() -> None:
    xtime_scraper = get_scraper(Platform.XTIME)
    cdk_scraper = get_scraper(Platform.CONNECT_CDK)

    assert xtime_scraper.platform_name == Platform.XTIME.value
    assert cdk_scraper.platform_name == Platform.CONNECT_CDK.value
    assert isinstance(xtime_scraper, PlatformScraper)
    assert isinstance(cdk_scraper, PlatformScraper)


def test_router_raises_for_unregistered_platform() -> None:
    with pytest.raises(ValueError) as exc:
        get_scraper(Platform.UNKNOWN)

    assert "unknown" in str(exc.value)
    assert "no scraper registered" in str(exc.value)


def test_router_registered_platforms_includes_both_slice_8_platforms() -> None:
    registered = set(registered_platforms())
    assert Platform.XTIME in registered
    assert Platform.CONNECT_CDK in registered
