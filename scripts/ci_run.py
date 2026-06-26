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
import json
import logging
import os
import sys
from pathlib import Path

import structlog

from vw_scraper.alerts import Severity, send_slack_alert
from vw_scraper.orchestrator import RunMetadata, run_daily
from vw_scraper.storage.drive import SyncSummary, build_drive_service, sync_outputs
from vw_scraper.storage.timeseries import append_to_timeseries

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = REPO_ROOT / "data" / "dealer_master.csv"
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR / "raw"
DEFAULT_TIMESERIES_PATH = DEFAULT_DATA_DIR / "processed" / "timeseries.parquet"

_ENV_SA_PATH = "VW_SCRAPER_SA_PATH"
_ENV_FOLDER_ID = "VW_SCRAPER_DRIVE_FOLDER_ID"

_DEGRADED_ERROR_RATE: float = 0.25

log = structlog.get_logger()


def _emit_alert(message: str, severity: Severity, *, alert_file: Path | None) -> None:
    """Fan one alert out to every configured channel.

    1. Slack webhook via `send_slack_alert` — a clean no-op when
       `VW_SCRAPER_SLACK_WEBHOOK` is unset (kept for back-compat / optional use).
    2. A JSON alert file the GitHub Actions workflow reads to open or update the
       rolling `scraper-health` issue. Skipped when `alert_file` is None (e.g.
       local runs). ci_run emits at most one alert per run, so a plain
       overwrite is sufficient.
    """
    send_slack_alert(message, severity=severity)
    if alert_file is None:
        return
    try:
        alert_file.parent.mkdir(parents=True, exist_ok=True)
        alert_file.write_text(
            json.dumps({"severity": severity, "message": message}) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("ci_alert_file_write_failed", path=str(alert_file), error=str(exc))


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
        "--timeseries-path",
        type=Path,
        default=DEFAULT_TIMESERIES_PATH,
        help="Path to the master timeseries parquet "
        "(default: data/processed/timeseries.parquet). Appended after the "
        "scrape so the data-repo push carries a current processed layer.",
    )
    parser.add_argument(
        "--skip-timeseries",
        action="store_true",
        help="Write raw JSONL only; don't append to the master parquet.",
    )
    parser.add_argument(
        "--alert-file",
        type=Path,
        default=None,
        help="If set, write a JSON {severity, message} here on a degraded or "
        "failed run for the workflow to turn into a GitHub issue.",
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
    # Drive sync is now optional. When both env vars are set, we sync; when
    # neither is set, we skip silently (the GH Actions workflow handles
    # persistence by pushing to a separate data repo). Half-configured state
    # — one var set, the other not — is still an error to catch typos.
    drive_configured = bool(sa_path_raw) and bool(folder_id)
    if bool(sa_path_raw) != bool(folder_id):
        which = _ENV_SA_PATH if not sa_path_raw else _ENV_FOLDER_ID
        print(
            f"Drive env vars are half-set: {which} is missing. "
            "Set both to enable Drive sync, or neither to skip it.",
            file=sys.stderr,
        )
        return 1

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
        _emit_alert(
            f"Daily scrape failed before writing JSONL: {exc}",
            severity="error",
            alert_file=args.alert_file,
        )
        return 1

    # --- Phase 1.5: append to the master time-series -------------------------
    # The cloud runner is the only place the processed layer is built, so the
    # data-repo push (next workflow step) has a current parquet to ship. The
    # workflow seeds today's runner with the existing parquet first, so this
    # accumulates across days. Idempotent on observation_date (purge-and-
    # replace), so re-runs are safe. A parquet failure must not lose the raw
    # JSONL we just wrote, so it's logged, not fatal.
    if not args.skip_timeseries:
        jsonl_path = (
            args.output_dir / metadata.observation_date.isoformat() / "observations.jsonl"
        )
        try:
            args.timeseries_path.parent.mkdir(parents=True, exist_ok=True)
            rows_written = append_to_timeseries(jsonl_path, args.timeseries_path)
            log.info(
                "ci_timeseries_appended",
                rows_written=rows_written,
                parquet_path=str(args.timeseries_path),
            )
        except Exception as exc:  # noqa: BLE001 — raw JSONL is already on disk
            log.error("ci_timeseries_append_failed", error=str(exc))

    # --- Phase 2: sync to Drive (optional) ----------------------------------
    sync_summary: SyncSummary | None = None
    if drive_configured:
        assert sa_path_raw is not None  # narrowed by drive_configured
        assert folder_id is not None
        sa_path = Path(sa_path_raw).expanduser()
        try:
            service = build_drive_service(sa_path)
            sync_summary = sync_outputs(service, args.data_dir, folder_id)
        except Exception as exc:  # noqa: BLE001 — alert; data is on an ephemeral runner
            log.error("ci_drive_sync_failed", error=str(exc))
            _emit_alert(
                (
                    f"Drive sync failed after scrape "
                    f"(run_id={metadata.run_id}, date={metadata.observation_date}): {exc}"
                ),
                severity="error",
                alert_file=args.alert_file,
            )
            return 1
    else:
        log.info("ci_drive_sync_skipped", reason="env vars unset")

    # --- Phase 3: threshold check + alerting --------------------------------
    attempted = metadata.dealers_attempted
    error_rate = metadata.error_count / attempted if attempted > 0 else 0.0

    log.info(
        "ci_run_summary",
        attempted=attempted,
        success_count=metadata.success_count,
        error_count=metadata.error_count,
        error_rate=error_rate,
        uploaded=sync_summary.uploaded if sync_summary else None,
        skipped=sync_summary.skipped if sync_summary else None,
    )

    if attempted > 0 and metadata.success_count == 0:
        _emit_alert(
            (
                f"All {attempted} dealers failed "
                f"(run_id={metadata.run_id}, date={metadata.observation_date})."
            ),
            severity="error",
            alert_file=args.alert_file,
        )
        return 2

    if error_rate > _DEGRADED_ERROR_RATE:
        _emit_alert(
            (
                f"Degraded run: {metadata.error_count}/{attempted} dealers failed "
                f"({error_rate:.0%}) "
                f"(run_id={metadata.run_id}, date={metadata.observation_date})."
            ),
            severity="warning",
            alert_file=args.alert_file,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
