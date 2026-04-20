"""Tests for vw_scraper.storage.drive.

Everything runs against a MagicMock Drive service — no network, no real Google
auth. The Drive client is built from discovery docs, so we can't easily spec a
mock against it; instead we configure method chains (files().list().execute(),
files().create().execute(), etc.) directly on MagicMocks and assert on call
arguments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from vw_scraper.storage import drive
from vw_scraper.storage.drive import (
    SyncSummary,
    _with_retry,
    build_drive_service,
    download_file,
    sync_outputs,
    upload_file,
)


def _make_http_error(status: int, reason: str = "test") -> HttpError:
    resp = httplib2.Response({"status": status, "reason": reason})
    return HttpError(resp, f"{status} {reason}".encode())


def _fake_service(
    *,
    list_responses: list[dict[str, Any]] | None = None,
    create_id: str = "created-id",
    update_id: str = "updated-id",
) -> MagicMock:
    """Build a MagicMock service with scripted list() responses.

    `list_responses` is consumed in order: each call to files().list().execute()
    returns the next dict. If exhausted, returns {"files": []} (absent).
    """
    service = MagicMock()
    responses = list(list_responses or [])

    def list_execute() -> dict[str, Any]:
        if responses:
            return responses.pop(0)
        return {"files": []}

    service.files.return_value.list.return_value.execute.side_effect = list_execute
    service.files.return_value.create.return_value.execute.return_value = {"id": create_id}
    service.files.return_value.update.return_value.execute.return_value = {"id": update_id}
    return service


def _write_file(path: Path, content: str = "hello", *, mtime: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mtime is not None:
        ts = mtime.timestamp()
        import os as _os
        _os.utime(path, (ts, ts))
    return path


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


def test_upload_file_creates_when_absent(tmp_path: Path) -> None:
    local = _write_file(tmp_path / "observations.jsonl", content="{}\n")
    service = _fake_service(create_id="file-new")

    file_id = upload_file(service, local, "folder-A")

    assert file_id == "file-new"
    service.files.return_value.create.assert_called_once()
    service.files.return_value.update.assert_not_called()
    kwargs = service.files.return_value.create.call_args.kwargs
    assert kwargs["body"]["name"] == "observations.jsonl"
    assert kwargs["body"]["parents"] == ["folder-A"]


def test_upload_file_updates_when_present(tmp_path: Path) -> None:
    local = _write_file(tmp_path / "observations.jsonl", content="{}\n")
    service = _fake_service(
        list_responses=[{"files": [{"id": "file-existing", "name": "observations.jsonl"}]}],
        update_id="file-existing",
    )

    file_id = upload_file(service, local, "folder-A")

    assert file_id == "file-existing"
    service.files.return_value.update.assert_called_once()
    service.files.return_value.create.assert_not_called()
    assert service.files.return_value.update.call_args.kwargs["fileId"] == "file-existing"


def test_upload_file_uses_remote_name_override(tmp_path: Path) -> None:
    local = _write_file(tmp_path / "local_name.jsonl", content="{}\n")
    service = _fake_service()

    upload_file(service, local, "folder-A", remote_name="renamed.jsonl")

    list_query = service.files.return_value.list.call_args.kwargs["q"]
    assert "name = 'renamed.jsonl'" in list_query
    assert service.files.return_value.create.call_args.kwargs["body"]["name"] == "renamed.jsonl"


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


def test_download_file_writes_atomically(tmp_path: Path) -> None:
    service = MagicMock()
    target = tmp_path / "nested" / "timeseries.parquet"

    class FakeDownloader:
        def __init__(self, fh: Any, request: Any, chunksize: int) -> None:
            self._fh = fh
            self._calls = 0

        def next_chunk(self) -> tuple[object, bool]:
            self._calls += 1
            if self._calls == 1:
                self._fh.write(b"chunk-1;")
                return (object(), False)
            self._fh.write(b"chunk-2")
            return (object(), True)

    with patch.object(drive, "MediaIoBaseDownload", FakeDownloader):
        download_file(service, "drive-file-id", target)

    assert target.read_bytes() == b"chunk-1;chunk-2"
    # No leftover tempfile alongside the target.
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


def test_download_file_cleans_up_tempfile_on_error(tmp_path: Path) -> None:
    service = MagicMock()
    target = tmp_path / "out.bin"

    class BoomDownloader:
        def __init__(self, fh: Any, request: Any, chunksize: int) -> None:
            pass

        def next_chunk(self) -> tuple[object, bool]:
            raise RuntimeError("boom")

    with patch.object(drive, "MediaIoBaseDownload", BoomDownloader):
        with pytest.raises(RuntimeError, match="boom"):
            download_file(service, "drive-file-id", target)

    assert not target.exists()
    assert list(target.parent.iterdir()) == []


# ---------------------------------------------------------------------------
# sync_outputs
# ---------------------------------------------------------------------------


def test_sync_outputs_creates_missing_folders(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_file(data_dir / "raw" / "2026-04-19" / "observations.jsonl", content="{}\n")
    _write_file(data_dir / "raw" / "2026-04-19" / "run_metadata.json", content="{}\n")

    # Every list call returns "not found" so the sync creates folders + files.
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.side_effect = [
        {"id": "folder-raw"},
        {"id": "folder-2026-04-19"},
        {"id": "file-obs"},
        {"id": "file-meta"},
    ]

    summary = sync_outputs(service, data_dir, "root-folder")

    assert summary.folders_created == 2
    assert summary.uploaded == 2
    assert summary.skipped == 0
    assert summary.files_scanned == 2

    # Folder creates should carry the right parents in order: raw under root,
    # then 2026-04-19 under raw.
    create_calls = service.files.return_value.create.call_args_list
    folder_bodies = [
        c.kwargs["body"]
        for c in create_calls
        if c.kwargs["body"].get("mimeType") == drive._FOLDER_MIME
    ]
    assert folder_bodies[0] == {
        "name": "raw",
        "parents": ["root-folder"],
        "mimeType": drive._FOLDER_MIME,
    }
    assert folder_bodies[1] == {
        "name": "2026-04-19",
        "parents": ["folder-raw"],
        "mimeType": drive._FOLDER_MIME,
    }


def test_sync_outputs_skips_when_remote_newer(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    local = _write_file(
        data_dir / "dealer_master.csv",
        content="a,b\n",
        mtime=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
    )
    remote_newer = (datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
                    + timedelta(minutes=5))

    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "file-existing",
                "name": "dealer_master.csv",
                "modifiedTime": remote_newer.isoformat().replace("+00:00", "Z"),
            }
        ]
    }

    summary = sync_outputs(service, data_dir, "root-folder")

    assert summary.skipped == 1
    assert summary.uploaded == 0
    service.files.return_value.update.assert_not_called()
    service.files.return_value.create.assert_not_called()


def test_sync_outputs_uploads_when_local_newer(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    local = _write_file(
        data_dir / "dealer_master.csv",
        content="a,b\n",
        mtime=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
    )
    remote_older = (datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
                    - timedelta(hours=1))

    existing_meta = {
        "id": "file-existing",
        "name": "dealer_master.csv",
        "modifiedTime": remote_older.isoformat().replace("+00:00", "Z"),
    }
    service = MagicMock()
    # First call from _sync_one_file checks existence; second call from
    # upload_file checks again to decide create-vs-update.
    service.files.return_value.list.return_value.execute.side_effect = [
        {"files": [existing_meta]},
        {"files": [existing_meta]},
    ]
    service.files.return_value.update.return_value.execute.return_value = {"id": "file-existing"}

    summary = sync_outputs(service, data_dir, "root-folder")

    assert summary.uploaded == 1
    assert summary.skipped == 0
    service.files.return_value.update.assert_called_once()


def test_sync_outputs_skips_dotfiles_and_pycache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_file(data_dir / ".DS_Store", content="junk")
    _write_file(data_dir / "__pycache__" / "junk.pyc", content="junk")
    _write_file(data_dir / "real.txt", content="hello")

    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.return_value = {"id": "file-real"}

    summary = sync_outputs(service, data_dir, "root-folder")

    assert summary.files_scanned == 1
    assert summary.uploaded == 1
    # Only one create total (the file); no __pycache__ folder create.
    create_names = [
        c.kwargs["body"]["name"] for c in service.files.return_value.create.call_args_list
    ]
    assert create_names == ["real.txt"]


def test_sync_outputs_raises_on_missing_dir(tmp_path: Path) -> None:
    service = MagicMock()
    with pytest.raises(FileNotFoundError):
        sync_outputs(service, tmp_path / "nope", "root-folder")


# ---------------------------------------------------------------------------
# _with_retry
# ---------------------------------------------------------------------------


def test_retry_succeeds_after_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _make_http_error(503)
        return "ok"

    assert _with_retry(fn) == "ok"
    assert len(calls) == 3


def test_retry_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        raise _make_http_error(503)

    with pytest.raises(HttpError):
        _with_retry(fn)
    assert len(calls) == drive._MAX_RETRY_ATTEMPTS


def test_retry_does_not_retry_4xx_except_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        raise _make_http_error(404)

    with pytest.raises(HttpError):
        _with_retry(fn)
    assert len(calls) == 1


def test_retry_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise _make_http_error(429)
        return "ok"

    assert _with_retry(fn) == "ok"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# build_drive_service
# ---------------------------------------------------------------------------


def test_build_drive_service_raises_on_missing_sa_path(tmp_path: Path) -> None:
    bogus = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError, match="VW_SCRAPER_SA_PATH"):
        build_drive_service(bogus)


# ---------------------------------------------------------------------------
# SyncSummary shape
# ---------------------------------------------------------------------------


def test_sync_summary_forbids_extra_fields() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        SyncSummary(uploaded=0, skipped=0, folders_created=0, files_scanned=0, bogus=1)  # type: ignore[call-arg]
