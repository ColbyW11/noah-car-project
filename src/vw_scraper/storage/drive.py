"""Google Drive sync: mirror local `data/` into a Drive folder.

Slice 7 of the build plan. Three public entry points:

- `upload_file` — create-or-replace one file in a Drive folder.
- `download_file` — fetch one Drive file to a local path atomically.
- `sync_outputs` — walk the whole local data tree and mirror structure + files
  into Drive, skipping files whose remote copy is newer-or-equal.

SPEC.md principle #5 (idempotent daily writes) extends here: re-running the
pipeline for the same date must not thrash Drive. We enforce that via
`modifiedTime` comparison — remote >= local means skip.

Drive's Python client is synchronous, so this module is too. The scraping
phase (async) finishes before sync runs, so there's no async win to chase.

Auth is a service account. Path from `VW_SCRAPER_SA_PATH`, root folder from
`VW_SCRAPER_DRIVE_FOLDER_ID`. The CLI (`scripts/sync_drive.py`) reads those
env vars; library functions here take concrete arguments to stay testable.
"""

from __future__ import annotations

import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import structlog
from google.oauth2 import service_account
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from googleapiclient.http import (  # type: ignore[import-untyped]
    MediaFileUpload,
    MediaIoBaseDownload,
)
from pydantic import BaseModel, ConfigDict

__all__ = [
    "SyncSummary",
    "build_drive_service",
    "download_file",
    "sync_outputs",
    "upload_file",
]

log = structlog.get_logger()

_FOLDER_MIME: str = "application/vnd.google-apps.folder"
_DRIVE_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive",)
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_ATTEMPTS: int = 5
_RETRY_BASE_DELAY_SECONDS: float = 1.0
_IGNORED_NAMES: frozenset[str] = frozenset({"__pycache__"})
_DOWNLOAD_CHUNK_SIZE: int = 1024 * 1024  # 1 MiB

# google-api-python-client has no type stubs on PyPI. Rather than sprinkle
# Any across signatures ad-hoc, alias it once. Runtime type is
# googleapiclient.discovery.Resource.
DriveService = Any

T = TypeVar("T")


class SyncSummary(BaseModel):
    """Result of a `sync_outputs` run. Matches RunMetadata's forbid-extra style."""

    model_config = ConfigDict(extra="forbid")

    uploaded: int
    skipped: int
    folders_created: int
    files_scanned: int


def build_drive_service(sa_path: Path) -> DriveService:
    """Build a Drive v3 service bound to the given service account JSON.

    Raises FileNotFoundError with a guided message when the file is missing —
    the most common misconfig is a freshly-cloned checkout where .env was
    never populated.
    """
    resolved = sa_path.expanduser()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Service account JSON not found at {resolved}. "
            "Copy .env.example to .env and set VW_SCRAPER_SA_PATH to the "
            "location of your Google Drive service account key."
        )
    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        str(resolved), scopes=list(_DRIVE_SCOPES)
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def upload_file(
    service: DriveService,
    local_path: Path,
    drive_folder_id: str,
    remote_name: str | None = None,
) -> str:
    """Create or replace a file in `drive_folder_id`. Returns the Drive file ID.

    If a file with the same name already exists in the folder, we `update` it
    (preserves file ID and Drive revision history). Otherwise `create`. Both
    paths are retry-wrapped.
    """
    name = remote_name or local_path.name
    bound = log.bind(local_path=str(local_path), drive_folder_id=drive_folder_id, name=name)

    existing = _find_child(service, drive_folder_id, name)
    media = MediaFileUpload(str(local_path), resumable=False)

    if existing is not None:
        file_id: str = existing["id"]
        bound.info("drive_upload_update", file_id=file_id)
        result = _with_retry(
            lambda: service.files()
            .update(fileId=file_id, media_body=media, fields="id")
            .execute()
        )
        return str(result["id"])

    bound.info("drive_upload_create")
    result = _with_retry(
        lambda: service.files()
        .create(
            body={"name": name, "parents": [drive_folder_id]},
            media_body=media,
            fields="id",
        )
        .execute()
    )
    return str(result["id"])


def download_file(service: DriveService, drive_file_id: str, local_path: Path) -> None:
    """Download a Drive file to `local_path` atomically.

    Chunked read into a tempfile next to the target, fsync, os.replace. A
    crashed download never leaves a truncated file at `local_path` — same
    invariant as `orchestrator._atomic_write_text`.
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)
    bound = log.bind(drive_file_id=drive_file_id, local_path=str(local_path))
    bound.info("drive_download_start")

    tmp = tempfile.NamedTemporaryFile(
        delete=False,
        dir=str(local_path.parent),
        prefix=local_path.name + ".",
        suffix=".tmp",
    )
    tmp_path = Path(tmp.name)
    try:
        request = service.files().get_media(fileId=drive_file_id)
        downloader = MediaIoBaseDownload(tmp, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
        done = False
        while not done:
            _status, done = _with_retry(downloader.next_chunk)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, local_path)
    except Exception:
        try:
            tmp.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    bound.info("drive_download_done")


def sync_outputs(
    service: DriveService,
    local_data_dir: Path,
    drive_root_folder_id: str,
) -> SyncSummary:
    """Mirror `local_data_dir` into Drive under `drive_root_folder_id`.

    Walks the tree depth-first. For each directory, ensures a matching Drive
    folder exists (creating if missing). For each file, compares local mtime
    against Drive `modifiedTime` and skips when the remote is newer-or-equal.
    Symlinks followed as-is; dotfiles and `__pycache__` skipped.

    Upload-only: remote files that no longer exist locally are NOT deleted.
    Drive retains history; surgery happens in a later slice if needed.
    """
    if not local_data_dir.exists():
        raise FileNotFoundError(f"local data dir does not exist: {local_data_dir}")
    if not local_data_dir.is_dir():
        raise NotADirectoryError(f"local data dir is not a directory: {local_data_dir}")

    bound = log.bind(local_data_dir=str(local_data_dir), drive_root=drive_root_folder_id)
    bound.info("drive_sync_start")

    folder_cache: dict[tuple[str, str], str] = {}
    counters = {"uploaded": 0, "skipped": 0, "folders_created": 0, "files_scanned": 0}

    def _walk(local_dir: Path, drive_parent_id: str) -> None:
        for entry in sorted(local_dir.iterdir()):
            if _should_skip(entry.name):
                continue
            if entry.is_dir():
                folder_id = _ensure_folder(
                    service, drive_parent_id, entry.name, folder_cache, counters
                )
                _walk(entry, folder_id)
            elif entry.is_file():
                counters["files_scanned"] += 1
                _sync_one_file(service, entry, drive_parent_id, counters)

    _walk(local_data_dir, drive_root_folder_id)

    summary = SyncSummary(
        uploaded=counters["uploaded"],
        skipped=counters["skipped"],
        folders_created=counters["folders_created"],
        files_scanned=counters["files_scanned"],
    )
    bound.info(
        "drive_sync_done",
        uploaded=summary.uploaded,
        skipped=summary.skipped,
        folders_created=summary.folders_created,
        files_scanned=summary.files_scanned,
    )
    return summary


def _should_skip(name: str) -> bool:
    return name.startswith(".") or name in _IGNORED_NAMES


def _sync_one_file(
    service: DriveService,
    local_path: Path,
    drive_parent_id: str,
    counters: dict[str, int],
) -> None:
    existing = _find_child(service, drive_parent_id, local_path.name)
    local_modified = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc)

    if existing is not None:
        remote_modified_raw = existing.get("modifiedTime")
        if remote_modified_raw is not None:
            remote_modified = _parse_drive_time(remote_modified_raw)
            if remote_modified >= local_modified:
                log.debug(
                    "drive_sync_skip_remote_newer",
                    local_path=str(local_path),
                    local_modified=local_modified.isoformat(),
                    remote_modified=remote_modified.isoformat(),
                )
                counters["skipped"] += 1
                return

    upload_file(service, local_path, drive_parent_id)
    counters["uploaded"] += 1


def _ensure_folder(
    service: DriveService,
    parent_id: str,
    name: str,
    cache: dict[tuple[str, str], str],
    counters: dict[str, int],
) -> str:
    key = (parent_id, name)
    if key in cache:
        return cache[key]
    existing = _find_child(service, parent_id, name, mime_type=_FOLDER_MIME)
    if existing is not None:
        folder_id: str = existing["id"]
    else:
        log.info("drive_folder_create", parent_id=parent_id, name=name)
        result = _with_retry(
            lambda: service.files()
            .create(
                body={"name": name, "parents": [parent_id], "mimeType": _FOLDER_MIME},
                fields="id",
            )
            .execute()
        )
        folder_id = str(result["id"])
        counters["folders_created"] += 1
    cache[key] = folder_id
    return folder_id


def _find_child(
    service: DriveService,
    parent_id: str,
    name: str,
    mime_type: str | None = None,
) -> dict[str, Any] | None:
    """Look up a child of `parent_id` by name. Returns the first match or None.

    We escape single quotes per Drive query syntax. Fields requested include
    `modifiedTime` so callers can compare against local mtime without a second
    round-trip.
    """
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    clauses = [f"name = '{escaped_name}'", f"'{parent_id}' in parents", "trashed = false"]
    if mime_type is not None:
        clauses.append(f"mimeType = '{mime_type}'")
    query = " and ".join(clauses)

    result = _with_retry(
        lambda: service.files()
        .list(
            q=query,
            fields="files(id, name, mimeType, modifiedTime)",
            pageSize=2,
            spaces="drive",
        )
        .execute()
    )
    files = result.get("files", [])
    if not files:
        return None
    first: dict[str, Any] = files[0]
    return first


def _parse_drive_time(raw: str) -> datetime:
    """Parse Drive's RFC 3339 `modifiedTime` into a tz-aware UTC datetime."""
    # Drive returns strings like '2026-04-19T14:03:22.123Z'. fromisoformat in
    # 3.11+ handles 'Z' suffix via a swap since 3.11 supports it directly.
    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = _MAX_RETRY_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY_SECONDS,
) -> T:
    """Call `fn()` with exponential backoff on transient Drive errors.

    Retries on HttpError with status in 429/5xx and on socket/connection
    errors. Non-retryable 4xx (e.g. 404 Not Found, 403 Forbidden) raise
    immediately — retrying them only wastes time. On final attempt failure,
    the last exception is re-raised.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            try:
                status_int = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_int = None
            if status_int not in _RETRYABLE_STATUSES or attempt == max_attempts:
                raise
            last_exc = exc
        except (socket.timeout, ConnectionError) as exc:
            if attempt == max_attempts:
                raise
            last_exc = exc

        delay = base_delay * (2 ** (attempt - 1))
        log.warning(
            "drive_retry",
            attempt=attempt,
            max_attempts=max_attempts,
            delay_seconds=delay,
            error=str(last_exc),
        )
        time.sleep(delay)

    # Unreachable: either returned on success or raised on final attempt.
    raise RuntimeError("drive retry loop exited without returning")  # pragma: no cover
