"""Shared HTTP constants and robots.txt checks.

Every scraper and discovery step sends the same `USER_AGENT` so dealers see a
single, identifiable client with a contact email (SPEC.md legal constraints).
"""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from . import __version__

USER_AGENT = (
    f"vw-oil-availability-scraper/{__version__} "
    "(+mailto:colby.warzecha@gmail.com; research; non-commercial)"
)


class RobotsCache:
    """Per-host robots.txt cache, scoped to a single run.

    `urllib.robotparser` is stdlib-only, handles caching cleanly when we own
    the keying, and avoids pulling a new dependency just for this check.
    """

    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    def is_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return True
        host_key = f"{parts.scheme}://{parts.netloc}"
        parser = self._parsers.get(host_key)
        if parser is None:
            parser = RobotFileParser()
            parser.set_url(f"{host_key}/robots.txt")
            try:
                parser.read()
            except Exception:
                # No robots.txt or unreachable → treat as permissive. We'd
                # rather over-fetch one page than silently skip a dealer.
                parser.parse([])
            self._parsers[host_key] = parser
        return parser.can_fetch(self._user_agent, url)
