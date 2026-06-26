"""CLI: rebuild the processed time-series parquet from retained raw partitions.

Walks every `<raw-dir>/YYYY-MM-DD/observations.jsonl` in date order and folds
it into `<parquet-path>` via the same `append_to_timeseries` the daily pipeline
uses. Idempotent per `observation_date` (purge-and-replace), so it's safe to
re-run and safe to point at a parquet that already has some days.

Use it to seed the data repo's processed layer from its `raw/` history, or to
recover after a gap:

    uv run python scripts/backfill_timeseries.py \
        --raw-dir /tmp/data-repo/raw \
        --timeseries-path /tmp/data-repo/processed/timeseries.parquet

Exit codes:
    0 — completed (prints how many partitions and rows were folded in).
    1 — invocation error (raw dir missing, no partitions found).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from vw_scraper.storage.timeseries import append_to_timeseries

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        required=True,
        help="Directory containing YYYY-MM-DD/observations.jsonl partitions.",
    )
    parser.add_argument(
        "--timeseries-path",
        type=Path,
        required=True,
        help="Parquet file to (re)build. Created if absent.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete the parquet first, so the result reflects only the raw "
        "partitions present (drops any dates no longer on disk).",
    )
    args = parser.parse_args(argv)

    if not args.raw_dir.is_dir():
        print(f"raw dir not found: {args.raw_dir}", file=sys.stderr)
        return 1

    partitions = sorted(
        p
        for p in args.raw_dir.iterdir()
        if p.is_dir() and _DATE_DIR.match(p.name) and (p / "observations.jsonl").exists()
    )
    if not partitions:
        print(f"no YYYY-MM-DD partitions under {args.raw_dir}", file=sys.stderr)
        return 1

    if args.rebuild and args.timeseries_path.exists():
        args.timeseries_path.unlink()

    args.timeseries_path.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    for partition in partitions:
        jsonl = partition / "observations.jsonl"
        rows = append_to_timeseries(jsonl, args.timeseries_path)
        total_rows += rows
        print(f"{partition.name}: +{rows} rows")

    print(
        f"\nDone. Folded {len(partitions)} partitions "
        f"({partitions[0].name} → {partitions[-1].name}), "
        f"{total_rows} successful rows into {args.timeseries_path}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
