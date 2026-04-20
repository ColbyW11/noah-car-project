"""CLI: mirror the local data/ directory into the configured Drive folder.

Reads the service account path and root folder ID from env vars (falling back
to the defaults documented in .env.example). Walks the local tree, creating
matching Drive subfolders and uploading files whose remote copy is older or
missing. Upload-only — never deletes remote files.

Exit codes:
    0 — sync completed (possibly zero uploads; idempotent reruns are expected).
    1 — config error (missing env var / service account file) or unrecoverable
        Drive API failure (bubbles up after retries exhaust).

    uv run python scripts/sync_drive.py
    uv run python scripts/sync_drive.py --data-dir data --debug
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import structlog

from vw_scraper.storage.drive import build_drive_service, sync_outputs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"

_ENV_SA_PATH = "VW_SCRAPER_SA_PATH"
_ENV_FOLDER_ID = "VW_SCRAPER_DRIVE_FOLDER_ID"


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
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Local directory to mirror into Drive (default: data/).",
    )
    parser.add_argument(
        "--sa-path",
        type=Path,
        default=None,
        help=f"Path to the service account JSON (default: ${_ENV_SA_PATH}).",
    )
    parser.add_argument(
        "--drive-folder-id",
        type=str,
        default=None,
        help=f"Root Drive folder ID to sync into (default: ${_ENV_FOLDER_ID}).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logs (includes per-file skip reasons).",
    )
    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)

    sa_path = args.sa_path or _env_path(_ENV_SA_PATH)
    if sa_path is None:
        print(
            f"{_ENV_SA_PATH} is not set and --sa-path was not given. "
            "Copy .env.example to .env and fill in VW_SCRAPER_SA_PATH.",
            file=sys.stderr,
        )
        return 1

    folder_id = args.drive_folder_id or os.environ.get(_ENV_FOLDER_ID)
    if not folder_id:
        print(
            f"{_ENV_FOLDER_ID} is not set and --drive-folder-id was not given.",
            file=sys.stderr,
        )
        return 1

    if not args.data_dir.exists():
        print(f"data dir not found: {args.data_dir}", file=sys.stderr)
        return 1

    try:
        service = build_drive_service(sa_path)
        summary = sync_outputs(service, args.data_dir, folder_id)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        print(f"drive sync failed: {exc}", file=sys.stderr)
        return 1

    print(summary.model_dump_json(indent=2))
    return 0


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var)
    if not raw:
        return None
    return Path(raw).expanduser()


if __name__ == "__main__":
    sys.exit(main())
