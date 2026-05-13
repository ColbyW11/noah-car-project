"""Shared HTTP constants and robots.txt checks.

Every scraper and discovery step sends the same `USER_AGENT` so dealers see a
single, identifiable client with a contact email (SPEC.md legal constraints).
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from . import __version__

# Identification follows the `Mozilla/... (compatible; <name>/<version>; +URL)`
# pattern used by major crawlers (Bingbot, Yandexbot, AhrefsBot). Pure
# product-token UAs ("vw-oil-availability-scraper/0.1.0 …") are dropped by
# dealer SPAs (Xtime's consumer.xtime.com served a degraded bundle that
# never finished rendering its button grid, blocking us from progressing
# past the iframe entry). The Mozilla-compatible prefix unblocks the
# rendering path while the `(compatible; <name>; +mailto:...)` portion
# keeps us honestly identifiable to dealers reading their access logs.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36 "
    f"(compatible; vw-oil-availability-scraper/{__version__}; "
    "+mailto:colby.warzecha@gmail.com; research; non-commercial)"
)

# Dealer CDNs (DealerOn/Varnish, Akamai) reject Python-urllib's default UA with
# 403, which `RobotFileParser` then interprets as "everything disallowed".
# Fetch with our identifying UA, then hand parsed lines to the parser directly.
_ROBOTS_FETCH_TIMEOUT_SECONDS = 10


class RobotsCache:
    """Per-host robots.txt cache, scoped to a single run."""

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
            parser = self._fetch_parser(host_key)
            self._parsers[host_key] = parser
        return parser.can_fetch(self._user_agent, url)

    def _fetch_parser(self, host_key: str) -> RobotFileParser:
        parser = RobotFileParser()
        parser.set_url(f"{host_key}/robots.txt")
        req = urllib.request.Request(
            f"{host_key}/robots.txt",
            headers={"User-Agent": self._user_agent},
        )
        try:
            with urllib.request.urlopen(req, timeout=_ROBOTS_FETCH_TIMEOUT_SECONDS) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                lines = resp.read().decode(charset, errors="replace").splitlines()
                parser.parse(lines)
        except urllib.request.HTTPError as exc:
            if exc.code in (401, 403):
                parser.disallow_all = True  # type: ignore[attr-defined]
            else:
                parser.parse([])
        except Exception:
            parser.parse([])
        return parser
