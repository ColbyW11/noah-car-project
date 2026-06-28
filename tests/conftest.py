"""Pytest fixtures and collection hooks for the vw_scraper test suite.

CLAUDE.md: "Live-site tests are marked `@pytest.mark.live` and skipped by
default. Run with `pytest -m live` manually." Pytest does not enforce this
via marker declaration alone — we need an explicit skip unless the user has
opted in with `-m live` on the command line.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    markexpr = config.getoption("-m", default="") or ""
    # `browser` tests launch a local headless browser (no network) and are
    # skipped by default for the same reason as `live` — they need the
    # Playwright chromium binary and add startup cost to the fast unit suite.
    skip_live = pytest.mark.skip(reason="live test; run with `pytest -m live`")
    skip_browser = pytest.mark.skip(reason="browser test; run with `pytest -m browser`")
    for item in items:
        if "live" not in markexpr and "live" in item.keywords:
            item.add_marker(skip_live)
        if "browser" not in markexpr and "browser" in item.keywords:
            item.add_marker(skip_browser)
