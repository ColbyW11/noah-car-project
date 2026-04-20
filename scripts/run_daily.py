"""CLI: run the daily scrape across every active dealer in the registry.

Writes one JSONL line per dealer to `<output-dir>/YYYY-MM-DD/observations.jsonl`
and a sibling `run_metadata.json` describing the run. Idempotent: re-running
the same date cleanly replaces the partition.

Exit codes:
    0 — run completed (any mix of success/error ScrapeResults; partial failures
        are expected and recorded in the JSONL).
    1 — invocation error (bad args, missing registry, I/O error).
    2 — run completed but *every* dealer failed — worth investigating.

    uv run python scripts/run_daily.py
    uv run python scripts/run_daily.py --output-dir /tmp/vw-smoke
    uv run python scripts/run_daily.py --concurrency 3 --headed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import structlog

from vw_scraper.orchestrator import run_daily

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "raw"


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
    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)

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

    # JSON summary to stdout so it can be piped into jq.
    print(metadata.model_dump_json(indent=2))

    if metadata.dealers_attempted > 0 and metadata.success_count == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
