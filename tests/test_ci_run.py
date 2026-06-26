"""Tests for scripts/ci_run.py — alert thresholds and exit codes.

All external moving parts (run_daily, Drive sync, send_slack_alert, service
construction) are monkeypatched onto the `scripts.ci_run` module's local
references. Each test builds a real `RunMetadata` via its pydantic model so
field names stay in sync with the actual orchestrator contract.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import ci_run
from vw_scraper.models import ScrapeResult, ScrapeStatus
from vw_scraper.orchestrator import RunMetadata
from vw_scraper.registry import Platform
from vw_scraper.storage.drive import SyncSummary


def _make_metadata(*, attempted: int, success: int) -> RunMetadata:
    now = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    return RunMetadata(
        run_id=uuid.uuid4().hex,
        observation_date=date(2026, 4, 20),
        start_ts=now,
        end_ts=now,
        duration_seconds=1.0,
        dealers_attempted=attempted,
        success_count=success,
        error_count=attempted - success,
        scraper_version="0.1.0",
        concurrency=5,
    )


def _sync_summary() -> SyncSummary:
    return SyncSummary(uploaded=0, skipped=0, folders_created=0, files_scanned=0)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Minimum env the CLI requires to start (we mock the actual pipeline)."""
    sa_file = tmp_path / "sa.json"
    sa_file.write_text("{}")
    monkeypatch.setenv("VW_SCRAPER_SA_PATH", str(sa_file))
    monkeypatch.setenv("VW_SCRAPER_DRIVE_FOLDER_ID", "folder-xyz")
    return tmp_path


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """A minimum valid-ish registry file — content doesn't matter, we mock
    `run_daily`. ci_run only checks existence before handing off."""
    path = tmp_path / "dealer_master.csv"
    path.write_text("dealer_code\n")
    return path


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Pre-wire the four collaborators ci_run touches. Tests override on top."""
    mocks = {
        "run_daily": MagicMock(),
        "asyncio_run": MagicMock(),
        "build_drive_service": MagicMock(return_value=MagicMock(name="drive_service")),
        "sync_outputs": MagicMock(return_value=_sync_summary()),
        "send_slack_alert": MagicMock(return_value=True),
        "append_to_timeseries": MagicMock(return_value=0),
    }
    # run_daily is async; ci_run calls it via asyncio.run(coro). We intercept
    # asyncio.run so tests can inject the RunMetadata directly without needing
    # to construct real coroutines. We also stub run_daily itself so the coro
    # produced by ci_run is harmless (never awaited — asyncio.run is mocked).
    monkeypatch.setattr(ci_run, "run_daily", mocks["run_daily"])
    monkeypatch.setattr(ci_run.asyncio, "run", mocks["asyncio_run"])
    monkeypatch.setattr(ci_run, "build_drive_service", mocks["build_drive_service"])
    monkeypatch.setattr(ci_run, "sync_outputs", mocks["sync_outputs"])
    monkeypatch.setattr(ci_run, "send_slack_alert", mocks["send_slack_alert"])
    # Stubbed by default so the unit tests don't read JSONL or write parquet;
    # the dedicated integration test below exercises the real append.
    monkeypatch.setattr(ci_run, "append_to_timeseries", mocks["append_to_timeseries"])
    return mocks


def _argv(registry: Path, tmp_path: Path) -> list[str]:
    return [
        "--registry",
        str(registry),
        "--data-dir",
        str(tmp_path),
        "--output-dir",
        str(tmp_path / "raw"),
        # Keep the parquet out of the real repo's data/ dir during tests.
        "--timeseries-path",
        str(tmp_path / "processed" / "timeseries.parquet"),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_run_does_not_alert_and_exits_0(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["send_slack_alert"].assert_not_called()
    wired["sync_outputs"].assert_called_once()


# ---------------------------------------------------------------------------
# Degraded run (> 25% error rate)
# ---------------------------------------------------------------------------


def test_degraded_run_alerts_warning_and_exits_0(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=3)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["send_slack_alert"].assert_called_once()
    call = wired["send_slack_alert"].call_args
    assert call.kwargs["severity"] == "warning"
    assert "2/5" in call.args[0]


def test_boundary_25_percent_does_not_alert(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """1/4 = 25%. The threshold is strictly > 25%, so no alert."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=4, success=3)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["send_slack_alert"].assert_not_called()


def test_just_over_25_percent_alerts(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """2/7 ≈ 28.6% — just over the threshold, should alert warning."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=7, success=5)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["send_slack_alert"].assert_called_once()
    assert wired["send_slack_alert"].call_args.kwargs["severity"] == "warning"


# ---------------------------------------------------------------------------
# All failed
# ---------------------------------------------------------------------------


def test_all_failed_alerts_error_and_exits_2(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=0)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 2
    wired["send_slack_alert"].assert_called_once()
    assert wired["send_slack_alert"].call_args.kwargs["severity"] == "error"
    assert "All 5" in wired["send_slack_alert"].call_args.args[0]


def test_zero_dealers_attempted_does_not_alert(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """An empty registry shouldn't be mistaken for 'all failed'."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=0, success=0)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["send_slack_alert"].assert_not_called()


# ---------------------------------------------------------------------------
# Exception paths
# ---------------------------------------------------------------------------


def test_run_daily_exception_alerts_error_and_exits_1(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].side_effect = RuntimeError("playwright exploded")

    rc = ci_run.main(_argv(registry, env))

    assert rc == 1
    wired["send_slack_alert"].assert_called_once()
    assert wired["send_slack_alert"].call_args.kwargs["severity"] == "error"
    # Drive sync must not run if the scrape blew up.
    wired["sync_outputs"].assert_not_called()


def test_sync_exception_alerts_error_and_exits_1(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)
    wired["sync_outputs"].side_effect = RuntimeError("drive 500")

    rc = ci_run.main(_argv(registry, env))

    assert rc == 1
    wired["send_slack_alert"].assert_called_once()
    assert wired["send_slack_alert"].call_args.kwargs["severity"] == "error"


def test_build_drive_service_exception_alerts_error_and_exits_1(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)
    wired["build_drive_service"].side_effect = FileNotFoundError("no sa file")

    rc = ci_run.main(_argv(registry, env))

    assert rc == 1
    wired["send_slack_alert"].assert_called_once()


# ---------------------------------------------------------------------------
# Config errors (invocation, not run errors — no alert; exit 1)
# ---------------------------------------------------------------------------


def test_missing_registry_returns_1_without_alert(
    env: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    rc = ci_run.main(_argv(tmp_path / "nope.csv", env))

    assert rc == 1
    wired["send_slack_alert"].assert_not_called()
    wired["asyncio_run"].assert_not_called()


def test_half_set_drive_env_returns_1_without_alert(
    monkeypatch: pytest.MonkeyPatch,
    registry: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    """Setting one Drive env var but not the other is almost always a typo —
    fail loudly rather than silently skipping the sync. (Setting neither is
    the supported skip-sync path.)"""
    monkeypatch.delenv("VW_SCRAPER_SA_PATH", raising=False)
    monkeypatch.setenv("VW_SCRAPER_DRIVE_FOLDER_ID", "folder-xyz")

    rc = ci_run.main(_argv(registry, tmp_path))

    assert rc == 1
    wired["send_slack_alert"].assert_not_called()
    wired["asyncio_run"].assert_not_called()


def test_neither_drive_env_set_skips_sync_and_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    registry: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    """The GH Actions deploy doesn't set Drive env vars — persistence is
    handled by a separate git-push step. ci_run.py must skip the Drive
    phase silently and still produce a successful exit code."""
    monkeypatch.delenv("VW_SCRAPER_SA_PATH", raising=False)
    monkeypatch.delenv("VW_SCRAPER_DRIVE_FOLDER_ID", raising=False)
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)

    rc = ci_run.main(_argv(registry, tmp_path))

    assert rc == 0
    wired["sync_outputs"].assert_not_called()
    wired["build_drive_service"].assert_not_called()
    wired["send_slack_alert"].assert_not_called()


def test_run_daily_receives_expected_kwargs(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """Lock in the argument plumbing from CLI flags to run_daily()."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)

    rc = ci_run.main(
        [
            "--registry",
            str(registry),
            "--data-dir",
            str(env),
            "--output-dir",
            str(env / "raw"),
            "--concurrency",
            "3",
        ]
    )

    assert rc == 0
    wired["run_daily"].assert_called_once()
    kwargs = wired["run_daily"].call_args.kwargs
    assert kwargs["registry_path"] == registry
    assert kwargs["output_dir"] == env / "raw"
    assert kwargs["concurrency"] == 3


# ---------------------------------------------------------------------------
# Time-series append (WS2: the cloud must build the processed layer)
# ---------------------------------------------------------------------------


def test_appends_to_timeseries_after_successful_scrape(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """The cloud run must append to the parquet so the data-repo push ships a
    current processed layer. We assert it's called with the day's JSONL and
    the configured parquet path."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["append_to_timeseries"].assert_called_once()
    jsonl_arg, parquet_arg = wired["append_to_timeseries"].call_args.args
    assert jsonl_arg == env / "raw" / "2026-04-20" / "observations.jsonl"
    assert parquet_arg == env / "processed" / "timeseries.parquet"


def test_skip_timeseries_flag_does_not_append(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)

    rc = ci_run.main(_argv(registry, env) + ["--skip-timeseries"])

    assert rc == 0
    wired["append_to_timeseries"].assert_not_called()


def test_timeseries_append_failure_does_not_fail_run(
    env: Path,
    registry: Path,
    wired: dict[str, MagicMock],
) -> None:
    """A parquet write failure must not lose the raw JSONL already on disk —
    it's logged, the run still exits 0."""
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)
    wired["append_to_timeseries"].side_effect = RuntimeError("parquet exploded")

    rc = ci_run.main(_argv(registry, env))

    assert rc == 0
    wired["sync_outputs"].assert_called_once()


def test_timeseries_append_real_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    registry: Path,
    tmp_path: Path,
) -> None:
    """End-to-end against the real append_to_timeseries: write a day's JSONL,
    run ci_run (Drive skipped), assert the parquet is created with the success
    rows. Guards the WS2 contract that the cloud builds the processed layer."""
    monkeypatch.delenv("VW_SCRAPER_SA_PATH", raising=False)
    monkeypatch.delenv("VW_SCRAPER_DRIVE_FOLDER_ID", raising=False)

    obs_date = date(2026, 4, 20)
    raw_dir = tmp_path / "raw" / obs_date.isoformat()
    raw_dir.mkdir(parents=True)
    ts = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    result = ScrapeResult(
        dealer_code="VW0004",
        observation_ts=ts,
        scrape_status=ScrapeStatus.SUCCESS,
        error_message=None,
        first_available_ts=ts,
        lead_time_hours=24.0,
        available_slots=[ts],
        slot_count=1,
        scheduling_flow_seconds=5.0,
        interaction_steps=3,
        platform=Platform.XTIME,
        source_payload_hash="sha256:cafebabe",
    )
    (raw_dir / "observations.jsonl").write_text(result.model_dump_json() + "\n")

    # Only the scrape is mocked; the real append runs against the JSONL above.
    monkeypatch.setattr(ci_run.asyncio, "run", lambda _coro: _make_metadata(attempted=1, success=1))
    monkeypatch.setattr(ci_run, "run_daily", MagicMock())

    parquet_path = tmp_path / "processed" / "timeseries.parquet"
    rc = ci_run.main(
        [
            "--registry", str(registry),
            "--data-dir", str(tmp_path),
            "--output-dir", str(tmp_path / "raw"),
            "--timeseries-path", str(parquet_path),
        ]
    )

    assert rc == 0
    assert parquet_path.exists()
    import polars as pl

    df = pl.read_parquet(parquet_path)
    assert df.height == 1
    assert df["dealer_code"].to_list() == ["VW0004"]


# ---------------------------------------------------------------------------
# Alert file (WS1: workflow turns this into a GitHub issue)
# ---------------------------------------------------------------------------


def test_alert_file_written_on_degraded_run(
    env: Path,
    registry: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=3)
    alert_file = tmp_path / "alert.json"

    rc = ci_run.main(_argv(registry, env) + ["--alert-file", str(alert_file)])

    assert rc == 0
    assert alert_file.exists()
    payload = json.loads(alert_file.read_text())
    assert payload["severity"] == "warning"
    assert "2/5" in payload["message"]


def test_alert_file_written_on_all_failed(
    env: Path,
    registry: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=0)
    alert_file = tmp_path / "alert.json"

    rc = ci_run.main(_argv(registry, env) + ["--alert-file", str(alert_file)])

    assert rc == 2
    payload = json.loads(alert_file.read_text())
    assert payload["severity"] == "error"


def test_no_alert_file_on_clean_run(
    env: Path,
    registry: Path,
    tmp_path: Path,
    wired: dict[str, MagicMock],
) -> None:
    wired["asyncio_run"].return_value = _make_metadata(attempted=5, success=5)
    alert_file = tmp_path / "alert.json"

    rc = ci_run.main(_argv(registry, env) + ["--alert-file", str(alert_file)])

    assert rc == 0
    assert not alert_file.exists()


