"""CLI: compute weekly metrics from the master timeseries + raw JSONL.

Reads `data/processed/timeseries.parquet` (per-observation summaries) and
`data/raw/<date>/observations.jsonl` (per-slot raw data). Writes charts
(PNGs) and a markdown summary to `data/reports/`.

The three core questions from SPEC.md:
    1. Network average lead time — mean hours from observation to first
       available oil-change slot, per day, across all successful dealers.
    2. Next-day appointment rate — % of dealers with first-available
       slot ≤ 48 hours of observation.
    3. Per-dealer scheduling flow seconds — wall-clock time from page
       load to slot list visible. Friction ranking for the eventual
       "fastest oil change near me" product.

Plus a bonus: availability heatmap by day-of-week × hour-of-day from
the raw slot lists.

    uv run python scripts/analyze.py
    uv run python scripts/analyze.py --window-days 30
    uv run python scripts/analyze.py --output-dir /tmp/report
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless backend; saves PNGs without a display

import matplotlib.pyplot as plt
import polars as pl

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARQUET = REPO_ROOT / "data" / "processed" / "timeseries.parquet"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "reports"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=DEFAULT_PARQUET,
        help="Path to the master timeseries parquet.",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory containing YYYY-MM-DD/observations.jsonl partitions.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write charts and summary markdown.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Number of days to include in the report (default: 7).",
    )
    args = parser.parse_args(argv)

    if not args.parquet.exists():
        print(
            f"timeseries parquet not found: {args.parquet}\n"
            f"Run `uv run python scripts/run_daily.py` at least once first.",
            file=sys.stderr,
        )
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    df = pl.read_parquet(args.parquet)
    cutoff = date.today() - timedelta(days=args.window_days - 1)
    df = df.filter(pl.col("observation_date") >= cutoff)

    if df.is_empty():
        print(
            f"No observations in the last {args.window_days} days.",
            file=sys.stderr,
        )
        return 1

    summary_lines: list[str] = [
        f"# VW oil-change availability — {args.window_days}-day report",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_",
        "",
        f"**Window:** {cutoff} → {date.today()}",
        f"**Observations:** {df.height} successful "
        f"({df['dealer_code'].n_unique()} distinct dealers)",
        "",
    ]

    # 1. Network avg lead time (per-day line chart + headline number)
    daily = (
        df.group_by("observation_date")
        .agg(
            pl.col("lead_time_hours").mean().alias("avg_lead_hours"),
            (pl.col("lead_time_hours") <= 48).cast(pl.Float64).mean().mul(100).alias(
                "next_day_rate_pct"
            ),
            pl.len().alias("dealers"),
        )
        .sort("observation_date")
    )
    overall_avg = float(df["lead_time_hours"].mean() or 0.0)
    next_day_pct = float(
        (df.filter(pl.col("lead_time_hours") <= 48).height / df.height) * 100
    )
    summary_lines += [
        "## Network average lead time",
        f"- **{overall_avg:.1f} hours** to first available oil change, "
        f"averaged across the network.",
        f"- **{next_day_pct:.0f}%** of dealers had a slot within 48 hours of "
        "observation (next-day rate).",
        "",
    ]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(
        daily["observation_date"].to_list(),
        daily["avg_lead_hours"].to_list(),
        marker="o",
    )
    ax.set_xlabel("Observation date")
    ax.set_ylabel("Mean lead time to first slot (hours)")
    ax.set_title(f"Network average lead time — last {args.window_days} days")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(args.output_dir / "lead_time_trend.png", dpi=120)
    plt.close(fig)

    # 2. Next-day rate over time (bar chart)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(
        [str(d) for d in daily["observation_date"].to_list()],
        daily["next_day_rate_pct"].to_list(),
        color="#3b82f6",
    )
    ax.set_xlabel("Observation date")
    ax.set_ylabel("% of dealers with slot ≤ 48h")
    ax.set_title(f"Next-day appointment rate — last {args.window_days} days")
    ax.set_ylim(0, 105)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(args.output_dir / "next_day_rate.png", dpi=120)
    plt.close(fig)

    # 3. Per-dealer friction ranking (avg scheduling_flow_seconds)
    by_dealer = (
        df.group_by("dealer_code")
        .agg(
            pl.col("scheduling_flow_seconds").mean().alias("avg_flow_s"),
            pl.col("lead_time_hours").mean().alias("avg_lead_h"),
            pl.col("slot_count").mean().alias("avg_slots"),
            pl.len().alias("observations"),
        )
        .sort("avg_flow_s")
    )
    summary_lines += [
        "## Per-dealer friction ranking",
        "Lower `avg_flow_s` = faster from page-load to slot list visible.",
        "Useful for the 'fastest oil change near me' product use case.",
        "",
        "| Dealer | Avg flow (s) | Avg lead (h) | Avg slots | # obs |",
        "|---|---|---|---|---|",
    ]
    for row in by_dealer.iter_rows(named=True):
        summary_lines.append(
            f"| {row['dealer_code']} | {row['avg_flow_s']:.1f} | "
            f"{row['avg_lead_h']:.1f} | {int(row['avg_slots'])} | "
            f"{row['observations']} |"
        )
    summary_lines.append("")

    fig, ax = plt.subplots(figsize=(8, max(3, by_dealer.height * 0.5)))
    dealers = by_dealer["dealer_code"].to_list()
    flow_s = by_dealer["avg_flow_s"].to_list()
    ax.barh(dealers, flow_s, color="#10b981")
    ax.set_xlabel("Average scheduling flow seconds")
    ax.set_title("Dealer friction ranking (lower = faster)")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(args.output_dir / "dealer_friction.png", dpi=120)
    plt.close(fig)

    # 4. Availability heatmap by day-of-week × hour-of-day (from raw JSONL)
    slot_buckets: Counter[tuple[int, int]] = Counter()
    jsonl_files = sorted(args.raw_dir.glob("*/observations.jsonl"))
    for jsonl_path in jsonl_files:
        partition_date = _parse_partition_date(jsonl_path)
        if partition_date is None or partition_date < cutoff:
            continue
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obs = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obs.get("scrape_status") != "success":
                continue
            for slot_iso in obs.get("available_slots", []):
                try:
                    slot = datetime.fromisoformat(slot_iso)
                except ValueError:
                    continue
                slot_buckets[(slot.weekday(), slot.hour)] += 1

    heatmap_generated = False
    if slot_buckets:
        # 7 (Mon-Sun) × 24 (hour) grid
        grid = [[0] * 24 for _ in range(7)]
        for (dow, hour), count in slot_buckets.items():
            grid[dow][hour] = count
        fig, ax = plt.subplots(figsize=(12, 4))
        im = ax.imshow(grid, aspect="auto", cmap="viridis")
        ax.set_xticks(range(24))
        ax.set_xticklabels([f"{h:02d}" for h in range(24)])
        ax.set_yticks(range(7))
        ax.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        ax.set_xlabel("Hour of day (slot start, dealer-local)")
        ax.set_ylabel("Day of week")
        ax.set_title("Available oil-change slots — last "
                     f"{args.window_days} days (count across all dealers)")
        fig.colorbar(im, ax=ax, label="Slot count")
        fig.tight_layout()
        fig.savefig(args.output_dir / "availability_heatmap.png", dpi=120)
        plt.close(fig)
        heatmap_generated = True

        total = sum(slot_buckets.values())
        summary_lines += [
            "## Availability heatmap",
            f"Total of **{total:,}** slot observations across "
            f"{args.window_days} days. See `availability_heatmap.png`.",
            "",
        ]
    else:
        summary_lines += [
            "## Availability heatmap",
            "_Skipped — no raw JSONL data in `data/raw/` for this window. "
            "Once `scripts/run_daily.py` writes a few days of partitions "
            "the heatmap will populate automatically._",
            "",
        ]

    files_section = [
        "## Files",
        "- `lead_time_trend.png` — network avg lead time over time",
        "- `next_day_rate.png` — % of dealers with next-day slots, per day",
        "- `dealer_friction.png` — per-dealer scheduling flow seconds",
    ]
    if heatmap_generated:
        files_section.append(
            "- `availability_heatmap.png` — day-of-week × hour-of-day slot count"
        )
    files_section.append("- `weekly_summary.md` — this document")
    files_section.append("")
    files_section.append(
        "Re-run with `uv run python scripts/analyze.py --window-days N`."
    )
    summary_lines += files_section
    (args.output_dir / "weekly_summary.md").write_text("\n".join(summary_lines))
    print(f"Wrote report to {args.output_dir}")
    print(f"  - 4 PNGs + weekly_summary.md")
    print(f"  - {df.height} observations across "
          f"{df['dealer_code'].n_unique()} dealers")
    return 0


def _parse_partition_date(jsonl_path: Path) -> date | None:
    """Pull `YYYY-MM-DD` from a path like `data/raw/2026-05-13/observations.jsonl`."""
    try:
        return date.fromisoformat(jsonl_path.parent.name)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
