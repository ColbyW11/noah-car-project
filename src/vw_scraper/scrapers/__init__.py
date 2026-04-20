"""Platform router: map a `Platform` enum value to its scraper instance.

Slice 8 introduced the router so adding a scraper is a single-line append to
`_REGISTRY` — the orchestrator no longer needs to know about individual
platform classes.
"""

from __future__ import annotations

from ..registry import Platform
from .base import PlatformScraper
from .connect_cdk import ConnectCdkScraper
from .xtime import XtimeScraper

_REGISTRY: dict[Platform, PlatformScraper] = {
    Platform.XTIME: XtimeScraper(),
    Platform.CONNECT_CDK: ConnectCdkScraper(),
}


def get_scraper(platform: Platform) -> PlatformScraper:
    """Return the scraper instance for `platform`.

    Raises `ValueError` for platforms without a registered scraper (including
    `Platform.UNKNOWN`) so misconfigurations surface loudly rather than
    degrading into silent no-ops at run time.
    """
    scraper = _REGISTRY.get(platform)
    if scraper is None:
        raise ValueError(
            f"no scraper registered for platform={platform.value!r}; "
            f"registered={sorted(p.value for p in _REGISTRY)}"
        )
    return scraper


def registered_platforms() -> list[Platform]:
    """Return the platforms that have a registered scraper. Useful for tests."""
    return list(_REGISTRY)


__all__ = ["PlatformScraper", "get_scraper", "registered_platforms"]
