"""Tests for scripts/backfill_timeseries.py.

Builds synthetic raw partitions in tmp_path and folds them with the real
append_to_timeseries, so the test guards the ordering + idempotency contract
the data-repo backfill relies on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from scripts import backfill_timeseries
from vw_scraper.models import ScrapeResult, ScrapeStatus
from vw_scraper.registry import Platform


def _write_partition(raw_dir: Path, day: str, dealer_codes: list[str]) -> None:
    part = raw_dir / day
    part.mkdir(parents=True)
    ts = datetime.fromisoformat(f"{day}T13:00:00+00:00").astimezone(timezone.utc)
    lines = [
        ScrapeResult(
            dealer_code=code,
            observation_ts=ts,
            scrape_status=ScrapeStatus.SUCCESS,
            error_message=None,
            first_available_ts=ts,
            lead_time_hours=24.0,
            available_slots=[ts],
            slot_count=1,
            scheduling_flow_seconds=5.0,
            interaction_steps=3,
            platform=Platform.XTIME,
            source_payload_hash="sha256:cafebabe",
        ).model_dump_json()
        for code in dealer_codes
    ]
    (part / "observations.jsonl").write_text("\n".join(lines) + "\n")


def test_backfill_folds_all_partitions_in_order(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _write_partition(raw, "2026-05-17", ["VW0002", "VW0004", "VW0005"])
    _write_partition(raw, "2026-05-27", ["VW0004", "VW0005"])  # Jeff gone
    parquet = tmp_path / "processed" / "timeseries.parquet"

    rc = backfill_timeseries.main(
        ["--raw-dir", str(raw), "--timeseries-path", str(parquet)]
    )

    assert rc == 0
    df = pl.read_parquet(parquet)
    assert df.height == 5
    assert sorted(df["observation_date"].unique().cast(pl.Utf8).to_list()) == [
        "2026-05-17",
        "2026-05-27",
    ]


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _write_partition(raw, "2026-05-17", ["VW0004", "VW0005"])
    parquet = tmp_path / "processed" / "timeseries.parquet"

    backfill_timeseries.main(["--raw-dir", str(raw), "--timeseries-path", str(parquet)])
    backfill_timeseries.main(["--raw-dir", str(raw), "--timeseries-path", str(parquet)])

    assert pl.read_parquet(parquet).height == 2  # not doubled


def test_backfill_missing_raw_dir_returns_1(tmp_path: Path) -> None:
    rc = backfill_timeseries.main(
        ["--raw-dir", str(tmp_path / "nope"), "--timeseries-path", str(tmp_path / "ts.parquet")]
    )
    assert rc == 1


def test_backfill_no_partitions_returns_1(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    rc = backfill_timeseries.main(
        ["--raw-dir", str(raw), "--timeseries-path", str(tmp_path / "ts.parquet")]
    )
    assert rc == 1
