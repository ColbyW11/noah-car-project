"""Tests for vw_scraper.storage.timeseries.

Everything runs against synthetic `ScrapeResult` objects serialized to JSONL
in `tmp_path` — no browser, no fixtures on disk. The parser itself is the
source of truth for the JSONL shape, so reusing `ScrapeResult.model_dump_json`
guarantees we test the real orchestrator → timeseries contract.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from vw_scraper.models import ScrapeResult, ScrapeStatus
from vw_scraper.registry import Platform
from vw_scraper.storage.timeseries import (
    append_to_timeseries,
    compute_daily_summary,
)


def _success(
    dealer_code: str,
    observation_ts: datetime,
    *,
    lead_time_hours: float | None = 24.0,
    scheduling_flow_seconds: float | None = 5.0,
) -> ScrapeResult:
    if lead_time_hours is not None:
        first = observation_ts + timedelta(hours=lead_time_hours)
        slots = [first]
        slot_count = 1
    else:
        first = None
        slots = []
        slot_count = 0
    return ScrapeResult(
        dealer_code=dealer_code,
        observation_ts=observation_ts,
        scrape_status=ScrapeStatus.SUCCESS,
        error_message=None,
        first_available_ts=first,
        lead_time_hours=lead_time_hours,
        available_slots=slots,
        slot_count=slot_count,
        scheduling_flow_seconds=scheduling_flow_seconds,
        interaction_steps=3,
        platform=Platform.XTIME,
        source_payload_hash="sha256:cafebabe",
    )


def _error(dealer_code: str, observation_ts: datetime, message: str = "TIMEOUT: fake") -> ScrapeResult:
    return ScrapeResult(
        dealer_code=dealer_code,
        observation_ts=observation_ts,
        scrape_status=ScrapeStatus.ERROR,
        error_message=message,
        first_available_ts=None,
        lead_time_hours=None,
        available_slots=[],
        slot_count=0,
        scheduling_flow_seconds=None,
        interaction_steps=0,
        platform=Platform.XTIME,
        source_payload_hash=None,
    )


def _write_jsonl(base_dir: Path, observation_date: date, results: list[ScrapeResult]) -> Path:
    partition = base_dir / observation_date.isoformat()
    partition.mkdir(parents=True, exist_ok=True)
    path = partition / "observations.jsonl"
    path.write_text("".join(r.model_dump_json() + "\n" for r in results))
    return path


# Reference timestamps: 2026-04-19 is a Sunday (weekday=6), 2026-04-20 is a Monday (weekday=0).
TS_DAY1 = datetime(2026, 4, 19, 14, 3, 22, tzinfo=timezone.utc)
TS_DAY2 = datetime(2026, 4, 20, 14, 3, 22, tzinfo=timezone.utc)


def test_first_write_creates_parquet_with_only_success_rows(tmp_path: Path) -> None:
    jsonl = _write_jsonl(
        tmp_path,
        TS_DAY1.date(),
        [
            _success("VW0001", TS_DAY1),
            _success("VW0002", TS_DAY1),
            _error("VW0003", TS_DAY1),
        ],
    )
    parquet = tmp_path / "timeseries.parquet"

    written = append_to_timeseries(jsonl, parquet)

    assert written == 2
    df = pl.read_parquet(parquet)
    assert df.height == 2
    assert sorted(df["dealer_code"].to_list()) == ["VW0001", "VW0002"]
    assert set(df["scrape_status"].to_list()) == {"success"}


def test_flattened_schema_drops_available_slots_and_adds_derived_fields(tmp_path: Path) -> None:
    jsonl = _write_jsonl(tmp_path, TS_DAY1.date(), [_success("VW0001", TS_DAY1)])
    parquet = tmp_path / "timeseries.parquet"

    append_to_timeseries(jsonl, parquet)
    df = pl.read_parquet(parquet)

    assert "available_slots" not in df.columns
    for derived in ("observation_date", "day_of_week", "hour_of_day"):
        assert derived in df.columns
    row = df.row(0, named=True)
    assert row["observation_date"] == TS_DAY1.date()
    assert row["day_of_week"] == TS_DAY1.weekday()  # Sunday → 6
    assert row["hour_of_day"] == TS_DAY1.hour  # 14


def test_append_new_day_preserves_prior_rows(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"
    day1_jsonl = _write_jsonl(tmp_path, TS_DAY1.date(), [_success("VW0001", TS_DAY1)])
    day2_jsonl = _write_jsonl(tmp_path, TS_DAY2.date(), [_success("VW0002", TS_DAY2)])

    append_to_timeseries(day1_jsonl, parquet)
    append_to_timeseries(day2_jsonl, parquet)

    df = pl.read_parquet(parquet)
    assert df.height == 2
    assert sorted(df["observation_date"].to_list()) == [TS_DAY1.date(), TS_DAY2.date()]


def test_rerun_same_day_replaces_rows_without_duplicating(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"

    first_batch = _write_jsonl(
        tmp_path,
        TS_DAY1.date(),
        [_success("VW0001", TS_DAY1), _success("VW0002", TS_DAY1)],
    )
    append_to_timeseries(first_batch, parquet)
    assert pl.read_parquet(parquet).height == 2

    # Rerun the same day with a DIFFERENT set of successful dealers.
    second_batch = _write_jsonl(
        tmp_path,
        TS_DAY1.date(),
        [
            _success("VW0003", TS_DAY1),
            _success("VW0004", TS_DAY1),
            _success("VW0005", TS_DAY1),
        ],
    )
    written = append_to_timeseries(second_batch, parquet)

    assert written == 3
    df = pl.read_parquet(parquet)
    assert df.height == 3
    assert sorted(df["dealer_code"].to_list()) == ["VW0003", "VW0004", "VW0005"]


def test_empty_day_purges_prior_rows_for_that_date(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"

    # Seed with data for day1 and day2.
    append_to_timeseries(
        _write_jsonl(tmp_path, TS_DAY1.date(), [_success("VW0001", TS_DAY1)]),
        parquet,
    )
    append_to_timeseries(
        _write_jsonl(tmp_path, TS_DAY2.date(), [_success("VW0002", TS_DAY2)]),
        parquet,
    )
    assert pl.read_parquet(parquet).height == 2

    # Re-run day1 with ALL errors — day1 rows should vanish, day2 untouched.
    all_errors = _write_jsonl(tmp_path, TS_DAY1.date(), [_error("VW0001", TS_DAY1)])
    written = append_to_timeseries(all_errors, parquet)

    assert written == 0
    df = pl.read_parquet(parquet)
    assert df.height == 1
    assert df["observation_date"].to_list() == [TS_DAY2.date()]


def test_empty_day_on_fresh_parquet_is_noop(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"
    jsonl = _write_jsonl(tmp_path, TS_DAY1.date(), [_error("VW0001", TS_DAY1)])

    written = append_to_timeseries(jsonl, parquet)

    assert written == 0
    assert not parquet.exists()


def test_empty_jsonl_without_valid_partition_dir_raises(tmp_path: Path) -> None:
    bogus_dir = tmp_path / "not-a-date"
    bogus_dir.mkdir()
    jsonl = bogus_dir / "observations.jsonl"
    jsonl.write_text(_error("VW0001", TS_DAY1).model_dump_json() + "\n")
    parquet = tmp_path / "timeseries.parquet"
    # Parquet must exist for the purge path to be triggered.
    append_to_timeseries(
        _write_jsonl(tmp_path, TS_DAY1.date(), [_success("VW0001", TS_DAY1)]),
        parquet,
    )

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        append_to_timeseries(jsonl, parquet)


def test_compute_daily_summary_computes_metrics_exactly(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"
    # lead_times [12, 24, 72] → avg 36.0, next_day_rate = 2/3 = 66.666...%
    # flow_seconds [3, 5, 7] → avg 5.0
    results = [
        _success("VW0001", TS_DAY1, lead_time_hours=12.0, scheduling_flow_seconds=3.0),
        _success("VW0002", TS_DAY1, lead_time_hours=24.0, scheduling_flow_seconds=5.0),
        _success("VW0003", TS_DAY1, lead_time_hours=72.0, scheduling_flow_seconds=7.0),
    ]
    append_to_timeseries(_write_jsonl(tmp_path, TS_DAY1.date(), results), parquet)

    summary = compute_daily_summary(parquet, TS_DAY1.date())

    assert summary.height == 1
    row = summary.row(0, named=True)
    assert row["observation_date"] == TS_DAY1.date()
    assert row["network_avg_lead_time_hours"] == pytest.approx(36.0)
    assert row["next_day_rate_pct"] == pytest.approx(200.0 / 3)
    assert row["avg_scheduling_flow_seconds"] == pytest.approx(5.0)
    assert row["successful_observations"] == 3


def test_compute_daily_summary_empty_date_returns_null_row(tmp_path: Path) -> None:
    parquet = tmp_path / "timeseries.parquet"
    append_to_timeseries(
        _write_jsonl(tmp_path, TS_DAY1.date(), [_success("VW0001", TS_DAY1)]),
        parquet,
    )

    summary = compute_daily_summary(parquet, TS_DAY2.date())

    assert summary.height == 1
    row = summary.row(0, named=True)
    assert row["observation_date"] == TS_DAY2.date()
    assert row["network_avg_lead_time_hours"] is None
    assert row["next_day_rate_pct"] is None
    assert row["avg_scheduling_flow_seconds"] is None
    assert row["successful_observations"] == 0


def test_compute_daily_summary_handles_zero_slot_successes(tmp_path: Path) -> None:
    """Successful observations with no slots contribute to the denominator
    of next_day_rate_pct (per SPEC.md line 127) but not the numerator."""
    parquet = tmp_path / "timeseries.parquet"
    results = [
        _success("VW0001", TS_DAY1, lead_time_hours=24.0),  # within 48h
        _success("VW0002", TS_DAY1, lead_time_hours=None),  # zero slots: null lead time
    ]
    append_to_timeseries(_write_jsonl(tmp_path, TS_DAY1.date(), results), parquet)

    summary = compute_daily_summary(parquet, TS_DAY1.date())
    row = summary.row(0, named=True)

    assert row["successful_observations"] == 2
    assert row["next_day_rate_pct"] == pytest.approx(50.0)  # 1 of 2
    assert row["network_avg_lead_time_hours"] == pytest.approx(24.0)  # null ignored
