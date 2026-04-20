"""Tests for vw_scraper.orchestrator.

All tests use tmp_path and a local `_RecordingScraper` that satisfies the
PlatformScraper Protocol without needing Playwright. The real browser /
XtimeScraper path is exercised by `tests/scrapers/test_xtime.py::@live` and
by manual `scripts/run_daily.py` runs.
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from playwright.async_api import Browser
from pydantic import TypeAdapter

from vw_scraper import __version__
from vw_scraper import orchestrator
from vw_scraper.models import ScrapeResult, ScrapeStatus
from vw_scraper.orchestrator import RunMetadata, run_daily
from vw_scraper.registry import DealerConfig, Platform

FROZEN_NOW = datetime(2026, 4, 19, 14, 3, 22, tzinfo=timezone.utc)
EXPECTED_DATE_DIR = "2026-04-19"


Outcome = ScrapeResult | Exception | Callable[[], Awaitable[ScrapeResult]]


class _RecordingScraper:
    """Fake PlatformScraper driven by a dict of per-dealer outcomes."""

    platform_name: str = Platform.XTIME.value

    def __init__(
        self,
        outcomes: dict[str, Outcome],
        *,
        concurrency_tracker: _ConcurrencyTracker | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._tracker = concurrency_tracker

    async def scrape(self, dealer: DealerConfig, browser: Browser) -> ScrapeResult:
        if self._tracker is not None:
            await self._tracker.enter()
        try:
            outcome = self._outcomes[dealer.dealer_code]
            if isinstance(outcome, Exception):
                raise outcome
            if callable(outcome):
                return await outcome()
            return outcome
        finally:
            if self._tracker is not None:
                self._tracker.exit()


class _ConcurrencyTracker:
    """Records the max number of concurrently-executing scrapes."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.02)  # give other coros a chance to stack up

    def exit(self) -> None:
        self.current -= 1


def _write_registry(path: Path, dealers: list[dict[str, Any]]) -> None:
    """Minimal CSV writer mirroring load_registry's expected schema."""
    fieldnames = [
        "dealer_code",
        "dealer_name",
        "dealer_url",
        "schedule_url",
        "platform",
        "zip",
        "region",
        "config_json",
        "active",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for dealer in dealers:
            row = {
                "dealer_code": dealer["dealer_code"],
                "dealer_name": dealer.get("dealer_name", dealer["dealer_code"]),
                "dealer_url": dealer.get("dealer_url", "https://example.com"),
                "schedule_url": dealer.get(
                    "schedule_url", "https://example.com/schedule"
                ),
                "platform": dealer.get("platform", Platform.XTIME.value),
                "zip": dealer.get("zip", ""),
                "region": dealer.get("region", ""),
                "config_json": dealer.get("config_json", "{}"),
                "active": "true" if dealer.get("active", True) else "false",
                "notes": dealer.get("notes", ""),
            }
            writer.writerow(row)


def _success_result(dealer_code: str, slot_iso: str = "2026-04-20T09:00:00-04:00") -> ScrapeResult:
    slot = datetime.fromisoformat(slot_iso)
    return ScrapeResult(
        dealer_code=dealer_code,
        observation_ts=FROZEN_NOW,
        scrape_status=ScrapeStatus.SUCCESS,
        error_message=None,
        first_available_ts=slot,
        lead_time_hours=(slot - FROZEN_NOW).total_seconds() / 3600,
        available_slots=[slot],
        slot_count=1,
        scheduling_flow_seconds=3.5,
        interaction_steps=4,
        platform=Platform.XTIME,
        source_payload_hash="sha256:deadbeef",
    )


def _error_result(dealer_code: str, error_message: str = "TIMEOUT: fake") -> ScrapeResult:
    return ScrapeResult(
        dealer_code=dealer_code,
        observation_ts=FROZEN_NOW,
        scrape_status=ScrapeStatus.ERROR,
        error_message=error_message,
        first_available_ts=None,
        lead_time_hours=None,
        available_slots=[],
        slot_count=0,
        scheduling_flow_seconds=None,
        interaction_steps=0,
        platform=Platform.XTIME,
        source_payload_hash=None,
    )


def _mock_browser() -> Browser:
    # The recording scraper never touches the Browser; a spec'd Mock keeps
    # the type checker happy without spinning up Playwright.
    return MagicMock(spec=Browser)


def _frozen_now() -> datetime:
    return FROZEN_NOW


def _read_jsonl(path: Path) -> list[ScrapeResult]:
    adapter = TypeAdapter(ScrapeResult)
    return [adapter.validate_json(line) for line in path.read_text().splitlines() if line]


async def test_run_daily_writes_one_jsonl_line_per_dealer(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(
        registry,
        [
            {"dealer_code": "VW0001"},
            {"dealer_code": "VW0002"},
            {"dealer_code": "VW0003"},
        ],
    )
    scraper = _RecordingScraper(
        {
            "VW0001": _success_result("VW0001"),
            "VW0002": _success_result("VW0002"),
            "VW0003": _success_result("VW0003"),
        }
    )

    metadata = await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    jsonl_path = tmp_path / "out" / EXPECTED_DATE_DIR / "observations.jsonl"
    results = _read_jsonl(jsonl_path)
    assert [r.dealer_code for r in results] == ["VW0001", "VW0002", "VW0003"]
    assert all(r.scrape_status is ScrapeStatus.SUCCESS for r in results)
    assert metadata.success_count == 3
    assert metadata.error_count == 0
    assert metadata.dealers_attempted == 3
    assert metadata.scraper_version == __version__


async def test_run_daily_isolates_per_dealer_failures(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(
        registry,
        [
            {"dealer_code": "VW0001"},
            {"dealer_code": "VW0002"},
            {"dealer_code": "VW0003"},
        ],
    )
    scraper = _RecordingScraper(
        {
            "VW0001": _success_result("VW0001"),
            "VW0002": RuntimeError("kaboom"),  # scraper contract violation
            "VW0003": _error_result("VW0003", "PARSE: synthetic"),
        }
    )

    metadata = await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    results = _read_jsonl(tmp_path / "out" / EXPECTED_DATE_DIR / "observations.jsonl")
    by_code = {r.dealer_code: r for r in results}
    assert by_code["VW0001"].scrape_status is ScrapeStatus.SUCCESS
    assert by_code["VW0002"].scrape_status is ScrapeStatus.ERROR
    assert by_code["VW0002"].error_message is not None
    assert by_code["VW0002"].error_message.startswith("UNEXPECTED:")
    assert "kaboom" in by_code["VW0002"].error_message
    assert by_code["VW0003"].scrape_status is ScrapeStatus.ERROR
    assert by_code["VW0003"].error_message == "PARSE: synthetic"
    assert metadata.success_count == 1
    assert metadata.error_count == 2


async def test_run_daily_emits_error_for_unknown_platform(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(
        registry,
        [
            {"dealer_code": "VW0001", "platform": Platform.XTIME.value},
            {"dealer_code": "VW0002", "platform": Platform.UNKNOWN.value},
        ],
    )
    scraper = _RecordingScraper({"VW0001": _success_result("VW0001")})

    metadata = await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    results = _read_jsonl(tmp_path / "out" / EXPECTED_DATE_DIR / "observations.jsonl")
    by_code = {r.dealer_code: r for r in results}
    assert by_code["VW0001"].scrape_status is ScrapeStatus.SUCCESS
    assert by_code["VW0002"].scrape_status is ScrapeStatus.ERROR
    assert by_code["VW0002"].platform is Platform.UNKNOWN
    msg = by_code["VW0002"].error_message
    assert msg is not None and msg.startswith("UNEXPECTED: no scraper for platform=")
    assert metadata.error_count == 1


async def test_run_daily_is_idempotent_for_same_date(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(registry, [{"dealer_code": "VW0001"}, {"dealer_code": "VW0002"}])
    scraper_map: Mapping[Platform, Any] = {
        Platform.XTIME: _RecordingScraper(
            {
                "VW0001": _success_result("VW0001"),
                "VW0002": _success_result("VW0002"),
            }
        )
    }

    out = tmp_path / "out"
    meta1 = await run_daily(
        registry_path=registry,
        output_dir=out,
        scraper_map=scraper_map,
        browser=_mock_browser(),
        now=_frozen_now,
    )
    meta2 = await run_daily(
        registry_path=registry,
        output_dir=out,
        scraper_map=scraper_map,
        browser=_mock_browser(),
        now=_frozen_now,
    )

    partition = out / EXPECTED_DATE_DIR
    jsonl_path = partition / "observations.jsonl"
    lines = jsonl_path.read_text().splitlines()
    assert len(lines) == 2, "re-run must replace, not append"
    # No stragglers from the atomic-write temp file.
    stragglers = list(partition.glob("*.tmp"))
    assert stragglers == []
    assert meta1.dealers_attempted == meta2.dealers_attempted == 2


async def test_run_daily_respects_concurrency_semaphore(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(
        registry,
        [{"dealer_code": f"VW000{i}"} for i in range(1, 5)],
    )
    tracker = _ConcurrencyTracker()
    scraper = _RecordingScraper(
        {f"VW000{i}": _success_result(f"VW000{i}") for i in range(1, 5)},
        concurrency_tracker=tracker,
    )

    await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        concurrency=2,
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    assert tracker.peak == 2, f"peak concurrency {tracker.peak} exceeds semaphore limit"


async def test_run_daily_times_out_misbehaving_scraper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorten the 65s production cap so the test finishes fast. The production
    # value is a policy choice; the code path is what matters here.
    monkeypatch.setattr(orchestrator, "_HARD_TIMEOUT_SECONDS", 0.3)

    registry = tmp_path / "registry.csv"
    _write_registry(registry, [{"dealer_code": "VW0001"}])

    async def _hang() -> ScrapeResult:
        await asyncio.sleep(5.0)
        return _success_result("VW0001")  # never reached

    scraper = _RecordingScraper({"VW0001": _hang})

    metadata = await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    results = _read_jsonl(tmp_path / "out" / EXPECTED_DATE_DIR / "observations.jsonl")
    assert len(results) == 1
    assert results[0].scrape_status is ScrapeStatus.ERROR
    msg = results[0].error_message
    assert msg is not None and msg.startswith("TIMEOUT: orchestrator hard cap")
    assert metadata.error_count == 1


async def test_run_metadata_matches_jsonl_counts(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(
        registry,
        [
            {"dealer_code": "VW0001"},
            {"dealer_code": "VW0002"},
            {"dealer_code": "VW0003", "platform": Platform.UNKNOWN.value},
        ],
    )
    scraper = _RecordingScraper(
        {
            "VW0001": _success_result("VW0001"),
            "VW0002": _error_result("VW0002", "NAVIGATION: fake"),
        }
    )

    metadata = await run_daily(
        registry_path=registry,
        output_dir=tmp_path / "out",
        scraper_map={Platform.XTIME: scraper},
        browser=_mock_browser(),
        now=_frozen_now,
    )

    partition = tmp_path / "out" / EXPECTED_DATE_DIR
    line_count = sum(1 for _ in (partition / "observations.jsonl").open())
    assert line_count == 3
    assert metadata.success_count + metadata.error_count == line_count
    assert metadata.dealers_attempted == line_count

    metadata_blob = json.loads((partition / "run_metadata.json").read_text())
    assert metadata_blob["run_id"] == metadata.run_id
    assert metadata_blob["observation_date"] == EXPECTED_DATE_DIR
    assert metadata_blob["scraper_version"] == __version__


async def test_run_daily_rejects_invalid_concurrency(tmp_path: Path) -> None:
    registry = tmp_path / "registry.csv"
    _write_registry(registry, [{"dealer_code": "VW0001"}])

    with pytest.raises(ValueError, match="concurrency"):
        await run_daily(
            registry_path=registry,
            output_dir=tmp_path / "out",
            concurrency=0,
            scraper_map={Platform.XTIME: _RecordingScraper({})},
            browser=_mock_browser(),
            now=_frozen_now,
        )


def test_run_metadata_round_trips() -> None:
    metadata = RunMetadata(
        run_id="abc123",
        observation_date=FROZEN_NOW.date(),
        start_ts=FROZEN_NOW,
        end_ts=FROZEN_NOW,
        duration_seconds=0.0,
        dealers_attempted=1,
        success_count=1,
        error_count=0,
        scraper_version=__version__,
        concurrency=5,
    )
    payload = metadata.model_dump_json()
    restored = RunMetadata.model_validate_json(payload)
    assert restored == metadata
