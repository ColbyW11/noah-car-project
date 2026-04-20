"""CLI: unattended daily pipeline — scrape, sync to Drive, alert on trouble.

Designed for GitHub Actions. Wraps the existing building blocks:

    run_daily  -> sync_outputs -> (threshold check) -> send_slack_alert

The slice-9 deliverable is this orchestrator plus a workflow YAML that calls
it. Threshold logic lives here (not in YAML) so it's unit-testable and so
the workflow stays short.

Alerts fire on four signals, keyed off the existing `RunMetadata`:

1. Any uncaught exception escaping `run_daily` — severity=error, exit 1.
2. Any uncaught exception escaping Drive sync  — severity=error, exit 1.
   (The ephemeral CI runner is the only copy of today's data once `run_daily`
   finishes, so a sync failure means data loss and is treated as critical.)
3. All dealers failed                         — severity=error, exit 2.
4. >25% dealers failed (but not all)          — severity=warning, exit 0.
   Matches the "degraded run" threshold in SPEC.md §Failure Handling.

Exit codes mirror `scripts/run_daily.py` so a single `if-failure` step in the
workflow YAML can react to either script uniformly.

    uv run python scripts/ci_run.py
    uv run python scripts/ci_run.py --output-dir /tmp/vw-ci-smoke --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import structlog

from vw_scraper.alerts import send_slack_alert
from vw_scraper.orchestrator import RunMetadata, run_daily
from vw_scraper.storage.drive import build_drive_service, sync_outputs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = REPO_ROOT / "data" / "dealer_master.csv"
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR / "raw"

_ENV_SA_PATH = "VW_SCRAPER_SA_PATH"
_ENV_FOLDER_ID = "VW_SCRAPER_DRIVE_FOLDER_ID"

_DEGRADED_ERROR_RATE: float = 0.25

log = structlog.get_logger()


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
        default=DEFAULT_REGISTRY,
        help="Path to dealer_master.csv (default: data/dealer_master.csv).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Local data root that will be mirrored to Drive (default: data/).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for per-date JSONL partitions (default: data/raw).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent dealer scrapes (default: 5).",
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

    sa_path_raw = os.environ.get(_ENV_SA_PATH)
    folder_id = os.environ.get(_ENV_FOLDER_ID)
    if not sa_path_raw:
        print(f"{_ENV_SA_PATH} is not set.", file=sys.stderr)
        return 1
    if not folder_id:
        print(f"{_ENV_FOLDER_ID} is not set.", file=sys.stderr)
        return 1
    sa_path = Path(sa_path_raw).expanduser()

    # --- Phase 1: scrape -----------------------------------------------------
    try:
        metadata: RunMetadata = asyncio.run(
            run_daily(
                registry_path=args.registry,
                output_dir=args.output_dir,
                concurrency=args.concurrency,
            )
        )
    except Exception as exc:  # noqa: BLE001 — alert and translate to exit code
        log.error("ci_run_daily_failed", error=str(exc))
        send_slack_alert(
            f"Daily scrape failed before writing JSONL: {exc}",
            severity="error",
        )
        return 1

    # --- Phase 2: sync to Drive ---------------------------------------------
    try:
        service = build_drive_service(sa_path)
        sync_summary = sync_outputs(service, args.data_dir, folder_id)
    except Exception as exc:  # noqa: BLE001 — alert and exit; data is on an ephemeral runner
        log.error("ci_drive_sync_failed", error=str(exc))
        send_slack_alert(
            (
                f"Drive sync failed after scrape "
                f"(run_id={metadata.run_id}, date={metadata.observation_date}): {exc}"
            ),
            severity="error",
        )
        return 1

    # --- Phase 3: threshold check + alerting --------------------------------
    attempted = metadata.dealers_attempted
    error_rate = metadata.error_count / attempted if attempted > 0 else 0.0

    log.info(
        "ci_run_summary",
        attempted=attempted,
        success_count=metadata.success_count,
        error_count=metadata.error_count,
        error_rate=error_rate,
        uploaded=sync_summary.uploaded,
        skipped=sync_summary.skipped,
    )

    if attempted > 0 and metadata.success_count == 0:
        send_slack_alert(
            (
                f"All {attempted} dealers failed "
                f"(run_id={metadata.run_id}, date={metadata.observation_date})."
            ),
            severity="error",
        )
        return 2

    if error_rate > _DEGRADED_ERROR_RATE:
        send_slack_alert(
            (
                f"Degraded run: {metadata.error_count}/{attempted} dealers failed "
                f"({error_rate:.0%}) "
                f"(run_id={metadata.run_id}, date={metadata.observation_date})."
            ),
            severity="warning",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
