"""Tests for vw_scraper.alerts.

Uses the `urlopen=` dependency-injection seam to avoid any real network IO.
Mock responses are plain MagicMocks configured as context managers — the
stdlib `urllib.request.urlopen` returns a context manager when used in a
`with` block, so the mock must support `__enter__` / `__exit__`.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from vw_scraper import alerts


def _ctx(status: int) -> MagicMock:
    """Build a MagicMock that behaves like the object returned by urlopen()."""
    response = MagicMock()
    response.status = status
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = False
    return ctx


def test_sends_expected_payload_and_url() -> None:
    urlopen = MagicMock(return_value=_ctx(200))

    result = alerts.send_slack_alert(
        "dealer VW0001 failed",
        severity="error",
        webhook_url="https://hooks.slack.example/T000/B000/xyz",
        urlopen=urlopen,
    )

    assert result is True
    assert urlopen.call_count == 1
    request, = urlopen.call_args.args
    assert request.full_url == "https://hooks.slack.example/T000/B000/xyz"
    assert request.method == "POST"
    assert request.headers["Content-type"] == "application/json"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["text"] == ":rotating_light: dealer VW0001 failed"
    assert urlopen.call_args.kwargs["timeout"] == alerts._REQUEST_TIMEOUT_SECONDS


def test_skips_when_webhook_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VW_SCRAPER_SLACK_WEBHOOK", raising=False)
    urlopen = MagicMock()

    result = alerts.send_slack_alert("anything", severity="warning", urlopen=urlopen)

    assert result is False
    urlopen.assert_not_called()


def test_skips_when_webhook_env_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VW_SCRAPER_SLACK_WEBHOOK", "")
    urlopen = MagicMock()

    result = alerts.send_slack_alert("anything", severity="info", urlopen=urlopen)

    assert result is False
    urlopen.assert_not_called()


def test_reads_webhook_from_env_when_arg_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VW_SCRAPER_SLACK_WEBHOOK", "https://hooks.slack.example/env")
    urlopen = MagicMock(return_value=_ctx(200))

    result = alerts.send_slack_alert("hi", severity="info", urlopen=urlopen)

    assert result is True
    request, = urlopen.call_args.args
    assert request.full_url == "https://hooks.slack.example/env"


def test_retries_once_on_urlerror_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    urlopen = MagicMock(side_effect=[urllib.error.URLError("boom"), _ctx(200)])

    result = alerts.send_slack_alert(
        "retry me",
        severity="warning",
        webhook_url="https://hooks.slack.example/retry",
        urlopen=urlopen,
    )

    assert result is True
    assert urlopen.call_count == 2


def test_returns_false_on_persistent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    urlopen = MagicMock(side_effect=urllib.error.URLError("boom"))

    result = alerts.send_slack_alert(
        "doomed",
        severity="error",
        webhook_url="https://hooks.slack.example/fail",
        urlopen=urlopen,
    )

    assert result is False
    assert urlopen.call_count == alerts._MAX_ATTEMPTS


def test_non_2xx_retryable_status_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    urlopen = MagicMock(side_effect=[_ctx(503), _ctx(200)])

    result = alerts.send_slack_alert(
        "transient",
        severity="warning",
        webhook_url="https://hooks.slack.example/5xx",
        urlopen=urlopen,
    )

    assert result is True
    assert urlopen.call_count == 2


def test_non_retryable_4xx_returns_false_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    urlopen = MagicMock(return_value=_ctx(404))

    result = alerts.send_slack_alert(
        "wrong url",
        severity="warning",
        webhook_url="https://hooks.slack.example/404",
        urlopen=urlopen,
    )

    assert result is False
    assert urlopen.call_count == 1


def test_http_error_with_retryable_status_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    http_error = urllib.error.HTTPError(
        url="https://hooks.slack.example/429",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    urlopen = MagicMock(side_effect=[http_error, _ctx(200)])

    result = alerts.send_slack_alert(
        "rate limited",
        severity="warning",
        webhook_url="https://hooks.slack.example/429",
        urlopen=urlopen,
    )

    assert result is True
    assert urlopen.call_count == 2


def test_severity_emoji_map_covers_all_levels() -> None:
    assert set(alerts._SEVERITY_EMOJI.keys()) == {"info", "warning", "error"}


def test_explicit_webhook_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VW_SCRAPER_SLACK_WEBHOOK", "https://hooks.slack.example/env-var")
    urlopen = MagicMock(return_value=_ctx(200))

    explicit_url = "https://hooks.slack.example/explicit"
    alerts.send_slack_alert(
        "explicit wins",
        severity="info",
        webhook_url=explicit_url,
        urlopen=urlopen,
    )

    request, = urlopen.call_args.args
    assert request.full_url == explicit_url


def test_empty_string_webhook_arg_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty explicit webhook_url is not treated as 'opt in to env lookup'.

    We pass an empty string explicitly -> skip. Only `None` triggers env
    lookup. This keeps the contract honest: 'pass nothing, we'll find it'
    vs. 'I passed something, use it exactly'.
    """
    monkeypatch.setenv("VW_SCRAPER_SLACK_WEBHOOK", "https://hooks.slack.example/env")
    urlopen = MagicMock()

    result = alerts.send_slack_alert(
        "empty-string",
        severity="info",
        webhook_url="",
        urlopen=urlopen,
    )

    assert result is False
    urlopen.assert_not_called()


def test_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.time, "sleep", lambda _s: None)
    urlopen = MagicMock(side_effect=TimeoutError("slow"))

    result = alerts.send_slack_alert(
        "slow",
        severity="error",
        webhook_url="https://hooks.slack.example/slow",
        urlopen=urlopen,
    )

    assert result is False
    assert urlopen.call_count == alerts._MAX_ATTEMPTS


def test_non_network_exceptions_propagate() -> None:
    """We only swallow the specific network errors listed in except clauses.

    A bare ValueError raised by urlopen surfaces as-is so programmer errors
    (e.g. building a malformed Request) aren't hidden behind a silent False.
    """
    urlopen = MagicMock(side_effect=ValueError("programmer error"))

    with pytest.raises(ValueError):
        alerts.send_slack_alert(
            "bad",
            severity="info",
            webhook_url="https://hooks.slack.example/bad",
            urlopen=urlopen,
        )
