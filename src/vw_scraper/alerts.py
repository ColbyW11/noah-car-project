"""Slack webhook alerts for run-level failures and degraded runs.

Slice 9 of the build plan. Single public entry point:

- `send_slack_alert(message, severity)` — POST a short text message to the
  Slack webhook configured via `VW_SCRAPER_SLACK_WEBHOOK`.

Design notes:

- Uses stdlib `urllib.request`. No third-party HTTP dep for one POST.
- If the webhook env var is unset or empty, we log a warning and return False
  rather than raising — alerting is optional and a missing webhook must never
  crash a daily CI run.
- One retry on transient errors (URLError, 5xx response, connection reset).
  Slack webhooks either work or don't; we don't sit in a long retry loop
  inside a time-boxed CI job.
- The `urlopen` argument is a dependency-injection seam so tests can swap in
  a MagicMock without touching the network (same style as `tests/test_drive.py`).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Literal

import structlog

__all__ = ["Severity", "send_slack_alert"]

log = structlog.get_logger()

Severity = Literal["info", "warning", "error"]

_ENV_WEBHOOK: str = "VW_SCRAPER_SLACK_WEBHOOK"
_REQUEST_TIMEOUT_SECONDS: float = 10.0
_MAX_ATTEMPTS: int = 2
_RETRY_DELAY_SECONDS: float = 1.0
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_SEVERITY_EMOJI: dict[Severity, str] = {
    "info": ":information_source:",
    "warning": ":warning:",
    "error": ":rotating_light:",
}

UrlOpen = Callable[..., Any]


def send_slack_alert(
    message: str,
    severity: Severity,
    *,
    webhook_url: str | None = None,
    urlopen: UrlOpen | None = None,
) -> bool:
    """POST a one-line message to the configured Slack webhook.

    Returns True on a 2xx response, False if the webhook is unconfigured or
    the POST failed after one retry. Never raises.

    If `webhook_url` is None, falls back to `VW_SCRAPER_SLACK_WEBHOOK`. If the
    env var is unset or empty we log at WARNING and return False — alerting
    is an optional signal, not a hard dependency of the daily run.

    `urlopen` is a test seam; the default is `urllib.request.urlopen`.
    """
    resolved_url = webhook_url if webhook_url is not None else os.environ.get(_ENV_WEBHOOK, "")
    bound = log.bind(severity=severity, has_webhook=bool(resolved_url))

    if not resolved_url:
        bound.warning("slack_alert_skipped_no_webhook")
        return False

    opener: UrlOpen = urlopen if urlopen is not None else urllib.request.urlopen
    payload = {"text": f"{_SEVERITY_EMOJI[severity]} {message}"}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        resolved_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with opener(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 0))
                if 200 <= status < 300:
                    bound.info("slack_alert_sent", status=status, attempt=attempt)
                    return True
                last_error = f"HTTP {status}"
                # Retry only on known-transient statuses.
                if status not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS:
                    bound.error("slack_alert_failed", status=status, attempt=attempt)
                    return False
        except urllib.error.HTTPError as exc:
            last_error = f"HTTPError {exc.code}"
            if exc.code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS:
                bound.error("slack_alert_failed", status=exc.code, attempt=attempt)
                return False
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == _MAX_ATTEMPTS:
                bound.error("slack_alert_failed", error=last_error, attempt=attempt)
                return False

        bound.warning(
            "slack_alert_retry",
            attempt=attempt,
            max_attempts=_MAX_ATTEMPTS,
            delay_seconds=_RETRY_DELAY_SECONDS,
            error=last_error,
        )
        time.sleep(_RETRY_DELAY_SECONDS)

    # Unreachable — loop always returns. Kept to satisfy the type checker.
    return False
