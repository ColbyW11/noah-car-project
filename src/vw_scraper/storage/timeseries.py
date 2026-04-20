"""Processed time series: raw JSONL -> append-only Parquet.

`append_to_timeseries` flattens a day's `observations.jsonl` (written by
`orchestrator.run_daily`) into the processed-layer schema and merges it into
the master Parquet file. Re-running a date replaces that date's rows in
place, never duplicates — this is SPEC.md principle #5.

Only successful observations land here (SPEC.md line 75). Error observations
are still preserved in the raw JSONL. Downstream (Slice 10 analytics) only
needs the successful ones.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from ..models import ScrapeResult, ScrapeStatus

__all__ = ["append_to_timeseries", "compute_daily_summary"]


_PROCESSED_SCHEMA: dict[str, pl.DataType] = {
    "dealer_code": pl.Utf8(),
    "observation_ts": pl.Datetime(time_unit="us", time_zone="UTC"),
    "scrape_status": pl.Utf8(),
    "error_message": pl.Utf8(),
    "first_available_ts": pl.Datetime(time_unit="us", time_zone="UTC"),
    "lead_time_hours": pl.Float64(),
    "slot_count": pl.Int64(),
    "scheduling_flow_seconds": pl.Float64(),
    "interaction_steps": pl.Int64(),
    "platform": pl.Utf8(),
    "source_payload_hash": pl.Utf8(),
    "scraper_version": pl.Utf8(),
    "observation_date": pl.Date(),
    "day_of_week": pl.Int8(),
    "hour_of_day": pl.Int8(),
}

_NEXT_DAY_THRESHOLD_HOURS: float = 48.0


def append_to_timeseries(jsonl_path: Path, parquet_path: Path) -> int:
    """Flatten today's observations JSONL and merge into the master Parquet.

    Idempotent: any date already present in the parquet whose rows appear in
    this batch is fully replaced. If the batch is empty (every dealer errored
    on this date) we still purge the prior rows for that date — the JSONL is
    the source of truth, and "no successes today" must be observable.

    Returns the number of successful rows written from this batch.
    """
    observations = _read_observations(jsonl_path)
    successes = [o for o in observations if o.scrape_status is ScrapeStatus.SUCCESS]
    new_df = _flatten(successes)

    if successes:
        target_dates: list[date] = sorted(set(new_df["observation_date"].to_list()))
    else:
        target_dates = [_partition_date_from_path(jsonl_path)]

    if parquet_path.exists():
        existing = pl.read_parquet(parquet_path)
        purged = existing.filter(~pl.col("observation_date").is_in(target_dates))
        combined = pl.concat([purged, new_df]) if new_df.height else purged
    elif new_df.height:
        combined = new_df
    else:
        # First run, nothing to write and nothing to purge.
        return 0

    _atomic_write_parquet(parquet_path, combined)
    return new_df.height


def compute_daily_summary(parquet_path: Path, target: date) -> pl.DataFrame:
    """One-row DataFrame with the three SPEC.md metrics for `target`.

    Columns: observation_date, network_avg_lead_time_hours, next_day_rate_pct,
    avg_scheduling_flow_seconds, successful_observations. Null metrics when
    the date has no successful observations so daily summaries concat cleanly.
    """
    df = pl.read_parquet(parquet_path).filter(pl.col("observation_date") == target)
    if df.height == 0:
        return pl.DataFrame(
            {
                "observation_date": [target],
                "network_avg_lead_time_hours": [None],
                "next_day_rate_pct": [None],
                "avg_scheduling_flow_seconds": [None],
                "successful_observations": [0],
            },
            schema={
                "observation_date": pl.Date(),
                "network_avg_lead_time_hours": pl.Float64(),
                "next_day_rate_pct": pl.Float64(),
                "avg_scheduling_flow_seconds": pl.Float64(),
                "successful_observations": pl.UInt32(),
            },
        )
    return df.select(
        pl.lit(target).cast(pl.Date).alias("observation_date"),
        pl.col("lead_time_hours").mean().alias("network_avg_lead_time_hours"),
        (
            100.0
            * (pl.col("lead_time_hours") <= _NEXT_DAY_THRESHOLD_HOURS).sum()
            / pl.len()
        ).alias("next_day_rate_pct"),
        pl.col("scheduling_flow_seconds").mean().alias("avg_scheduling_flow_seconds"),
        pl.len().cast(pl.UInt32).alias("successful_observations"),
    )


def _read_observations(jsonl_path: Path) -> list[ScrapeResult]:
    lines = jsonl_path.read_text().splitlines()
    return [ScrapeResult.model_validate_json(line) for line in lines if line.strip()]


def _flatten(successes: Iterable[ScrapeResult]) -> pl.DataFrame:
    rows = [_row(result) for result in successes]
    if not rows:
        return pl.DataFrame(schema=_PROCESSED_SCHEMA)
    return pl.DataFrame(rows, schema=_PROCESSED_SCHEMA)


def _row(result: ScrapeResult) -> dict[str, object]:
    # Normalize to UTC so polars stores a single-tz column. The raw JSONL
    # keeps the dealer-local offset (CLAUDE.md: "Scraped slot times are
    # stored with their original timezone offset"). Conversion to analytics
    # UTC happens here, at the boundary.
    obs = result.observation_ts.astimezone(timezone.utc)
    first: datetime | None = (
        result.first_available_ts.astimezone(timezone.utc)
        if result.first_available_ts is not None
        else None
    )
    return {
        "dealer_code": result.dealer_code,
        "observation_ts": obs,
        "scrape_status": result.scrape_status.value,
        "error_message": result.error_message,
        "first_available_ts": first,
        "lead_time_hours": result.lead_time_hours,
        "slot_count": result.slot_count,
        "scheduling_flow_seconds": result.scheduling_flow_seconds,
        "interaction_steps": result.interaction_steps,
        "platform": result.platform.value,
        "source_payload_hash": result.source_payload_hash,
        "scraper_version": result.scraper_version,
        "observation_date": obs.date(),
        "day_of_week": obs.weekday(),
        "hour_of_day": obs.hour,
    }


def _partition_date_from_path(jsonl_path: Path) -> date:
    """Fallback date source when the batch is empty: parent dir is YYYY-MM-DD."""
    parent = jsonl_path.parent.name
    try:
        return date.fromisoformat(parent)
    except ValueError as exc:
        raise ValueError(
            f"Cannot infer observation date from empty JSONL at {jsonl_path}: "
            f"parent directory {parent!r} is not in YYYY-MM-DD format"
        ) from exc


def _atomic_write_parquet(path: Path, df: pl.DataFrame) -> None:
    """Tempfile + fsync + os.replace, same pattern as `orchestrator._atomic_write_text`.

    Readers see either the old parquet or the new one, never a truncated
    in-progress write. This is what makes rerunning a day safe — a crashed
    rewrite cannot corrupt the master table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        df.write_parquet(tmp_path)
        with tmp_path.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
