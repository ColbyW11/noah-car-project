"""Tests for vw_scraper.platform_detect.

Uses small synthetic HTML strings so these tests stay fast and stable even as
real dealer sites drift. Real dealer HTML snapshots live under
tests/fixtures/discovery/ and are exercised by test_discovery_fixtures.py.
"""

from __future__ import annotations

from vw_scraper.platform_detect import detect_platform_from_signals
from vw_scraper.registry import Platform


def test_detects_xtime_from_iframe_src() -> None:
    html = "<html><body><iframe src='https://schedule.xtime.com/'></iframe></body></html>"
    platform = detect_platform_from_signals(
        page_html=html,
        page_url="https://example-dealer.com/service",
        iframe_srcs=["https://schedule.xtime.com/vw0001"],
    )
    assert platform is Platform.XTIME


def test_detects_xtime_from_script_tag() -> None:
    html = (
        "<html><head>"
        "<script src='https://cdn.xtime.com/consumerschedulingfe/app.js'></script>"
        "</head><body></body></html>"
    )
    platform = detect_platform_from_signals(
        page_html=html,
        page_url="https://example-dealer.com/service",
        iframe_srcs=[],
    )
    assert platform is Platform.XTIME


def test_detects_mykaarma_from_url() -> None:
    platform = detect_platform_from_signals(
        page_html="<html><body>welcome</body></html>",
        page_url="https://app.mykaarma.com/dealer/abc/schedule",
        iframe_srcs=[],
    )
    assert platform is Platform.MYKAARMA


def test_detects_dealer_fx_from_iframe_src() -> None:
    platform = detect_platform_from_signals(
        page_html="<html><body></body></html>",
        page_url="https://example-dealer.com/service",
        iframe_srcs=["https://widget.dealer-fx.com/scheduling?d=VW1"],
    )
    assert platform is Platform.DEALER_FX


def test_returns_custom_when_inline_scheduler_without_vendor_signature() -> None:
    html = (
        '<html><body>'
        '<form><input type="date" name="appt" />'
        '<select class="time-slot"><option>8:00</option></select>'
        '</form></body></html>'
    )
    platform = detect_platform_from_signals(
        page_html=html,
        page_url="https://example-dealer.com/service/schedule",
        iframe_srcs=[],
    )
    assert platform is Platform.CUSTOM


def test_returns_unknown_when_no_signature_matches() -> None:
    platform = detect_platform_from_signals(
        page_html="<html><body><h1>Welcome</h1></body></html>",
        page_url="https://example-dealer.com/",
        iframe_srcs=[],
    )
    assert platform is Platform.UNKNOWN


def test_priority_xtime_wins_over_custom_signature() -> None:
    """A page with both vendor script AND inline date inputs classifies as
    the vendor — otherwise we'd mis-route scrapes to a nonexistent 'custom'
    scraper when Xtime's own widget happens to render <input type='date'>.
    """
    html = (
        '<html><body>'
        '<iframe src="https://schedule.xtime.com/x"></iframe>'
        '<input type="date" />'
        '</body></html>'
    )
    platform = detect_platform_from_signals(
        page_html=html,
        page_url="https://example-dealer.com/service",
        iframe_srcs=["https://schedule.xtime.com/x"],
    )
    assert platform is Platform.XTIME
