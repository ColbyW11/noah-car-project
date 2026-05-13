"""CLI: run the daily scrape across every active dealer in the registry.

Writes one JSONL line per dealer to `<output-dir>/YYYY-MM-DD/observations.jsonl`
and a sibling `run_metadata.json` describing the run, then appends successful
observations to `data/processed/timeseries.parquet`. Idempotent: re-running
the same date cleanly replaces both the JSONL partition and that date's rows
in the parquet.

Exit codes:
    0 — run completed (any mix of success/error ScrapeResults; partial failures
        are expected and recorded in the JSONL).
    1 — invocation error (bad args, missing registry, I/O error).
    2 — run completed but *every* dealer failed — worth investigating.

    uv run python scripts/run_daily.py
    uv run python scripts/run_daily.py --output-dir /tmp/vw-smoke
    uv run python scripts/run_daily.py --concurrency 3 --headed
    uv run python scripts/run_daily.py --skip-timeseries   # raw only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import structlog

from vw_scraper.orchestrator import run_daily
from vw_scraper.storage.timeseries import append_to_timeseries

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_TIMESERIES_PATH = REPO_ROOT / "data" / "processed" / "timeseries.parquet"


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=REGISTRY_CSV,
        help="Path to dealer_master.csv (default: data/dealer_master.csv).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write YYYY-MM-DD/ partitions (default: data/raw).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent dealer scrapes (default: 5).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (useful for debugging selectors).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logs.",
    )
    parser.add_argument(
        "--timeseries-path",
        type=Path,
        default=DEFAULT_TIMESERIES_PATH,
        help="Path to the master timeseries parquet "
        "(default: data/processed/timeseries.parquet).",
    )
    parser.add_argument(
        "--skip-timeseries",
        action="store_true",
        help="Write raw JSONL only; don't append to the master parquet.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Suppress macOS notification on degraded/failed runs.",
    )
    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)
    log = structlog.get_logger()

    if not args.registry.exists():
        print(f"registry not found: {args.registry}", file=sys.stderr)
        return 1

    try:
        metadata = asyncio.run(
            run_daily(
                registry_path=args.registry,
                output_dir=args.output_dir,
                concurrency=args.concurrency,
                headed=args.headed,
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        print(f"run_daily failed: {exc}", file=sys.stderr)
        return 1

    # Append successful observations to the master parquet — idempotent on
    # the observation_date so a same-day re-run replaces rather than
    # duplicates. We do this here (rather than inside the orchestrator) so
    # the orchestrator stays focused on scraping and so a parquet write
    # failure can't silently swallow the raw JSONL we just wrote.
    if not args.skip_timeseries:
        jsonl_path = args.output_dir / metadata.observation_date.isoformat() / "observations.jsonl"
        try:
            args.timeseries_path.parent.mkdir(parents=True, exist_ok=True)
            rows_written = append_to_timeseries(jsonl_path, args.timeseries_path)
            log.info(
                "timeseries_appended",
                rows_written=rows_written,
                parquet_path=str(args.timeseries_path),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("timeseries_append_failed", error=str(exc))
            # Don't fail the whole run — the raw JSONL is already on disk.

        # Regenerate the analytics report so `data/reports/` is always
        # current with the latest observation. ~1s of overhead. Failure
        # here is logged but doesn't fail the run.
        analyze_script = Path(__file__).parent / "analyze.py"
        if analyze_script.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(analyze_script)],
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    log.info("analytics_report_regenerated")
                else:
                    log.warning(
                        "analytics_report_nonzero_exit",
                        rc=result.returncode,
                        stderr=result.stderr[:200],
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("analytics_report_failed", error=str(exc))

    # JSON summary to stdout so it can be piped into jq.
    print(metadata.model_dump_json(indent=2))

    # macOS notification on failure or degradation. Silent on full success
    # — you only hear from the scraper when something needs attention.
    if not args.no_notify:
        _maybe_notify(metadata, log)

    if metadata.dealers_attempted > 0 and metadata.success_count == 0:
        return 2
    return 0


def _maybe_notify(metadata: "RunMetadata", log: "structlog.stdlib.BoundLogger") -> None:  # type: ignore[name-defined]
    """Fire a macOS banner via `osascript` when the run looks degraded.

    Silent on full success (every active dealer produced slot data) since
    a banner every morning trains you to ignore them. Fires when:

      - All dealers errored (`success_count == 0`): bright red title.
      - Some succeeded, some didn't (partial failure): yellow-flag title.

    Failures here are themselves swallowed — a notification problem
    shouldn't break the run.
    """
    attempted = metadata.dealers_attempted
    succeeded = metadata.success_count
    if attempted == 0:
        return
    if succeeded == attempted:
        return  # all dealers succeeded; nothing to alert on

    if succeeded == 0:
        title = f"VW scraper FAILED ({attempted} dealers)"
        msg = "Zero successful observations today. Check logs."
    else:
        title = f"VW scraper degraded ({succeeded}/{attempted})"
        msg = f"{attempted - succeeded} dealer(s) failed. Logs: ~/Library/Logs/vw-scraper.err.log"

    script = (
        f'display notification "{msg}" '
        f'with title "{title}" '
        f'sound name "Submarine"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_failed", error=str(exc))


if __name__ == "__main__":
    sys.exit(main())
