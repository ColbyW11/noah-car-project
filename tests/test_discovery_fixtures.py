"""Drift guard: every dealer row has a matching discovery fixture folder.

Catches the "I added a dealer to the CSV but forgot to capture fixtures" (or
the reverse) mistake that would otherwise only surface when a scraper runs.

Error-state dealers (robots disallow, hard timeout) legitimately have no HTML
snapshot — only `metadata.json` is required, and its `platform_detected`
matches the CSV's `platform`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vw_scraper.registry import Platform, load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "discovery"


@pytest.mark.parametrize("dealer", load_registry(REGISTRY_CSV), ids=lambda d: d.dealer_code)
def test_dealer_has_discovery_fixture(dealer) -> None:  # type: ignore[no-untyped-def]
    dealer_dir = FIXTURE_ROOT / dealer.dealer_code
    metadata_path = dealer_dir / "metadata.json"

    assert metadata_path.exists(), (
        f"Missing {metadata_path}. Every registered dealer needs a discovery "
        f"fixture; re-run scripts/discover_platforms.py for {dealer.dealer_code}."
    )

    metadata = json.loads(metadata_path.read_text())

    assert metadata["dealer_code"] == dealer.dealer_code
    assert metadata["platform_detected"] == dealer.platform.value, (
        f"CSV says platform={dealer.platform.value} but "
        f"{metadata_path} says platform_detected={metadata['platform_detected']}"
    )

    # HTML + screenshot are only guaranteed when the dealer loaded
    # successfully. Errors (robots.txt, timeout) skip those.
    if metadata.get("error") is None and dealer.platform is not Platform.UNKNOWN:
        assert (dealer_dir / "schedule_page.html").exists(), (
            f"Missing schedule_page.html for {dealer.dealer_code} — expected "
            "because discovery succeeded with a known platform."
        )
        assert (dealer_dir / "screenshot.png").exists(), (
            f"Missing screenshot.png for {dealer.dealer_code}."
        )
