"""CLI: report per-dealer success rate over the last N days.

Catches silent walker breakage that the per-run macOS notification
misses. The notification fires when a single run shows
`success_count < dealers_attempted` for that morning, but the bigger
risk is a steady drift — a dealer succeeded 6 days ago, errored 5 days
ago, and you didn't notice because nothing failed catastrophically.

This script reads `data/processed/timeseries.parquet` (successful
observations only — error observations live in the raw JSONL) and
prints a per-dealer summary. Exit code 0 if every active dealer has at
least one success in the window; exit code 1 if any active dealer is
silent. Run weekly via launchd or just before reviewing the data.

    uv run python scripts/health_check.py
    uv run python scripts/health_check.py --window-days 14
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from vw_scraper.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARQUET = REPO_ROOT / "data" / "processed" / "timeseries.parquet"
DEFAULT_REGISTRY = REPO_ROOT / "data" / "dealer_master.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=DEFAULT_PARQUET,
        help="Path to the master timeseries parquet.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="Path to dealer_master.csv (used to list active dealers).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7).",
    )
    args = parser.parse_args(argv)

    if not args.parquet.exists():
        print(
            f"timeseries parquet not found: {args.parquet}\n"
            "Run `uv run python scripts/run_daily.py` at least once first.",
            file=sys.stderr,
        )
        return 1

    active_dealers = [d.dealer_code for d in load_registry(args.registry)]
    cutoff = date.today() - timedelta(days=args.window_days - 1)
    df = pl.read_parquet(args.parquet).filter(
        pl.col("observation_date") >= cutoff
    )

    summary = (
        df.group_by("dealer_code")
        .agg(
            pl.len().alias("success_runs"),
            pl.col("observation_date").min().alias("first_success"),
            pl.col("observation_date").max().alias("last_success"),
            pl.col("slot_count").mean().alias("avg_slots"),
        )
        .sort("dealer_code")
    )
    seen_dealers = set(summary["dealer_code"].to_list())
    silent_dealers = [d for d in active_dealers if d not in seen_dealers]

    expected_runs = args.window_days  # one run per day, in theory

    print(f"Health check — last {args.window_days} days (since {cutoff})")
    print(f"Registry has {len(active_dealers)} active dealers.")
    print()
    print(f"{'Dealer':<8}  {'Runs':>5}  {'Last seen':<12}  {'Avg slots':>10}  Status")
    print("-" * 70)

    for code in active_dealers:
        if code in seen_dealers:
            row = summary.filter(pl.col("dealer_code") == code).to_dicts()[0]
            runs = row["success_runs"]
            last_seen = row["last_success"]
            avg_slots = row["avg_slots"]
            days_since = (date.today() - last_seen).days
            if days_since >= 2:
                status = f"⚠️  stale ({days_since}d ago)"
            elif runs < expected_runs * 0.8:
                status = f"⚠️  intermittent ({runs}/{expected_runs} runs)"
            else:
                status = "✓"
            print(
                f"{code:<8}  {runs:>5}  {str(last_seen):<12}  "
                f"{int(avg_slots):>10}  {status}"
            )

    for code in silent_dealers:
        print(
            f"{code:<8}  {0:>5}  {'never':<12}  {'-':>10}  "
            f"🚨 no successes in window"
        )

    if silent_dealers:
        print()
        print(
            f"FAILED: {len(silent_dealers)} dealer(s) had zero successes "
            f"in the last {args.window_days} days: {', '.join(silent_dealers)}"
        )
        return 1

    print()
    print(f"OK: all {len(active_dealers)} active dealers produced at least "
          "one observation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
