"""One-shot CLI: identify scheduling platforms for a batch of VW dealers.

Reads a plain-text file of dealer root URLs (one per line; anything after the
URL like a phone number is ignored), navigates each with Playwright, detects
the platform, saves HTML + screenshot fixtures, and updates
data/dealer_master.csv atomically.

This is the permanent artifact we re-run in batches of ~20 as new dealers are
added to the pilot (per SLICES.md post-Slice-10 plan).

Usage:
    uv run python scripts/discover_platforms.py \\
        --urls-file vwdealders_from_noah.txt \\
        --start-code VW0001
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from vw_scraper.http import USER_AGENT, RobotsCache
from vw_scraper.platform_detect import detect_platform
from vw_scraper.registry import Platform

DEALER_TIMEOUT_SECONDS = 60
NAVIGATION_TIMEOUT_MS = 30_000

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "discovery"

SCHEDULE_LINK_TEXT = re.compile(
    r"schedule\s+service|service\s+appointment|book\s+service|schedule\s+an?\s+appointment",
    re.IGNORECASE,
)
URL_TOKEN = re.compile(r"https?://\S+|(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", re.IGNORECASE)


log = structlog.get_logger()


@dataclass
class DiscoveryInput:
    dealer_code: str
    root_url: str
    schedule_url: str | None = None


@dataclass
class ConfigHints:
    zip_required: bool = False
    vehicle_selection_required: bool = False
    login_wall: bool = False
    iframe_embedded: bool = False
    iframe_src_pattern: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "zip_required": self.zip_required,
            "vehicle_selection_required": self.vehicle_selection_required,
            "login_wall": self.login_wall,
            "iframe_embedded": self.iframe_embedded,
        }
        if self.iframe_src_pattern:
            d["iframe_src_pattern"] = self.iframe_src_pattern
        return d


@dataclass
class DiscoveryResult:
    dealer_code: str
    root_url: str
    final_url: str = ""
    schedule_url: str = ""
    platform: Platform = Platform.UNKNOWN
    config_hints: ConfigHints = field(default_factory=ConfigHints)
    iframe_srcs: list[str] = field(default_factory=list)
    error: str | None = None


def parse_urls_file(path: Path) -> list[str]:
    """Extract a URL token from each non-empty line; ignore trailing junk."""
    urls: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = URL_TOKEN.search(line)
        if not match:
            log.warning("skip_line_no_url", line=line)
            continue
        token = match.group(0).strip(".,;")
        if not token.lower().startswith(("http://", "https://")):
            token = "https://" + token
        urls.append(token)
    return urls


def _normalize_host(url: str) -> str:
    stripped = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    stripped = stripped.split("/", 1)[0]
    return stripped.lower()


def _assign_dealer_codes(urls: list[str], start_code: str) -> list[DiscoveryInput]:
    m = re.match(r"^([A-Za-z]+)(\d+)$", start_code)
    if not m:
        raise ValueError(f"start-code must match PREFIX<digits>, got {start_code!r}")
    prefix, digits = m.group(1), m.group(2)
    width = len(digits)
    start = int(digits)
    return [
        DiscoveryInput(
            dealer_code=f"{prefix}{start + i:0{width}d}",
            root_url=url,
        )
        for i, url in enumerate(urls)
    ]


async def _find_schedule_url(page: Page) -> str | None:
    """Heuristically locate the service scheduling link on the dealer homepage."""
    try:
        link = page.get_by_role("link", name=SCHEDULE_LINK_TEXT).first
        href = await link.get_attribute("href", timeout=2_000)
        if href:
            return href
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    for selector in (
        'a[href*="schedule-service" i]',
        'a[href*="service-scheduling" i]',
        'a[href*="schedule" i][href*="service" i]',
        'a[href*="appointment" i]',
        'a[href*="schedule" i]',
    ):
        try:
            href = await page.locator(selector).first.get_attribute(
                "href", timeout=1_500
            )
            if href:
                return href
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return None


async def _extract_config_hints(page: Page) -> ConfigHints:
    hints = ConfigHints()
    html_lower = (await page.content()).lower()

    if re.search(r'name\s*=\s*["\'][^"\']*zip', html_lower) or "postal code" in html_lower:
        hints.zip_required = True
    if (
        "select your vehicle" in html_lower
        or re.search(r'name\s*=\s*["\'][^"\']*(?:vehicle|vin|year|make|model)', html_lower)
    ):
        hints.vehicle_selection_required = True
    if (
        'type="password"' in html_lower
        or "/login" in page.url.lower()
        or "/signin" in page.url.lower()
    ):
        hints.login_wall = True

    top_host = _normalize_host(page.url)
    analytics_hosts = (
        "googletagmanager.com",
        "google-analytics.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googletagservices.com",
        "adsrvr.org",
        "amazon-adsystem.com",
        "criteo.com",
        "criteo.net",
        "facebook.com",
        "hotjar.com",
        "segment.io",
        "cloudflareinsights.com",
        "bing.com",
    )
    for frame in page.frames:
        if not frame.url or frame == page.main_frame:
            continue
        if frame.url.startswith("data:") or frame.url.startswith("about:"):
            continue
        frame_host = _normalize_host(frame.url)
        if not frame_host or frame_host == top_host:
            continue
        if any(frame_host.endswith(h) for h in analytics_hosts):
            continue
        hints.iframe_embedded = True
        hints.iframe_src_pattern = frame_host
        break
    return hints


async def _discover_one(
    input_: DiscoveryInput,
    browser: Browser,
    robots: RobotsCache,
) -> DiscoveryResult:
    result = DiscoveryResult(
        dealer_code=input_.dealer_code,
        root_url=input_.root_url,
    )
    dealer_dir = FIXTURE_ROOT / input_.dealer_code
    dealer_dir.mkdir(parents=True, exist_ok=True)

    bound_log = log.bind(dealer_code=input_.dealer_code, root_url=input_.root_url)

    if not robots.is_allowed(input_.root_url):
        bound_log.warning("robots_disallow_root")
        result.error = "NAVIGATION: robots.txt disallows root URL"
        _write_metadata(dealer_dir, result)
        return result

    context: BrowserContext | None = None
    try:
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        await page.goto(input_.root_url, wait_until="domcontentloaded")
        (dealer_dir / "root.html").write_text(await page.content())

        schedule_url = input_.schedule_url
        if not schedule_url:
            schedule_url = await _find_schedule_url(page)
        if schedule_url:
            if not schedule_url.lower().startswith(("http://", "https://")):
                schedule_url = urljoin(page.url, schedule_url)
            if not robots.is_allowed(schedule_url):
                bound_log.warning("robots_disallow_schedule", url=schedule_url)
                result.schedule_url = schedule_url
                result.error = "NAVIGATION: robots.txt disallows schedule URL"
                _write_metadata(dealer_dir, result)
                return result
            bound_log.info("navigate_schedule", url=schedule_url)
            try:
                await page.goto(schedule_url, wait_until="networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                bound_log.warning("networkidle_timeout_falling_back_to_domcontentloaded")
                await page.goto(schedule_url, wait_until="domcontentloaded")
            result.schedule_url = page.url
        else:
            bound_log.warning("no_schedule_link_found")
            result.schedule_url = ""

        result.final_url = page.url

        schedule_html = await page.content()
        (dealer_dir / "schedule_page.html").write_text(schedule_html)
        try:
            await page.screenshot(
                path=str(dealer_dir / "screenshot.png"),
                full_page=True,
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            bound_log.warning("screenshot_timeout")

        result.platform = await detect_platform(page)
        result.config_hints = await _extract_config_hints(page)
        # iframe_src_pattern is only a useful hint when the platform is
        # unidentified — otherwise the captured "external iframe" is usually
        # just an ad/consent tracker, not the scheduler.
        if result.platform is not Platform.UNKNOWN:
            result.config_hints.iframe_src_pattern = None
        result.iframe_srcs = [
            frame.url for frame in page.frames if frame.url and frame != page.main_frame
        ]

        bound_log.info(
            "dealer_done",
            platform=result.platform.value,
            final_url=result.final_url,
        )
    except PlaywrightTimeoutError as exc:
        bound_log.error("dealer_timeout", error=str(exc))
        result.error = f"TIMEOUT: {exc}"
    except PlaywrightError as exc:
        bound_log.error("dealer_navigation_error", error=str(exc))
        result.error = f"NAVIGATION: {exc}"
    except Exception as exc:
        bound_log.error("dealer_unexpected_error", error=str(exc), tb=traceback.format_exc())
        result.error = f"UNEXPECTED: {exc}"
    finally:
        if context is not None:
            await context.close()

    _write_metadata(dealer_dir, result)
    return result


def _write_metadata(dealer_dir: Path, result: DiscoveryResult) -> None:
    metadata = {
        "dealer_code": result.dealer_code,
        "root_url": result.root_url,
        "final_url": result.final_url,
        "schedule_url": result.schedule_url,
        "platform_detected": result.platform.value,
        "config_hints": result.config_hints.to_dict(),
        "iframe_srcs": result.iframe_srcs,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "user_agent": USER_AGENT,
        "error": result.error,
    }
    (dealer_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


async def run_discovery(inputs: list[DiscoveryInput]) -> list[DiscoveryResult]:
    results: list[DiscoveryResult] = []
    robots = RobotsCache()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for input_ in inputs:
                try:
                    result = await asyncio.wait_for(
                        _discover_one(input_, browser, robots),
                        timeout=DEALER_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "dealer_hard_timeout",
                        dealer_code=input_.dealer_code,
                        seconds=DEALER_TIMEOUT_SECONDS,
                    )
                    result = DiscoveryResult(
                        dealer_code=input_.dealer_code,
                        root_url=input_.root_url,
                        error=f"TIMEOUT: exceeded {DEALER_TIMEOUT_SECONDS}s hard cap",
                    )
                    _write_metadata(FIXTURE_ROOT / input_.dealer_code, result)
                results.append(result)
        finally:
            await browser.close()
    return results


def _derive_dealer_name(root_url: str) -> str:
    host = _normalize_host(root_url)
    host = re.sub(r"^www\.", "", host)
    host = host.split(".", 1)[0]
    return host.replace("-", " ").title() or host


def write_registry_csv(
    csv_path: Path,
    results: list[DiscoveryResult],
) -> None:
    """Update `data/dealer_master.csv` in place, atomically.

    Rows matching a `dealer_code` in `results` are replaced; unknown dealer
    codes are left untouched. Write is atomic via tempfile + os.replace.
    """
    results_by_code = {r.dealer_code: r for r in results}

    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise RuntimeError(f"{csv_path}: missing header")
        rows = list(reader)

    for row in rows:
        code = row.get("dealer_code", "")
        if code not in results_by_code:
            continue
        result = results_by_code[code]

        row["dealer_name"] = _derive_dealer_name(result.root_url)
        row["dealer_url"] = result.root_url
        row["schedule_url"] = result.schedule_url
        row["platform"] = result.platform.value
        row["zip"] = row.get("zip", "")
        row["region"] = row.get("region", "")
        row["config_json"] = json.dumps(result.config_hints.to_dict())
        row["active"] = row.get("active", "true") or "true"
        row["notes"] = result.error or ""

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        delete=False,
        dir=str(csv_path.parent),
        prefix=csv_path.name + ".",
        suffix=".tmp",
    )
    try:
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, csv_path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--urls-file",
        type=Path,
        required=True,
        help="Path to a text file with one dealer URL per line.",
    )
    parser.add_argument(
        "--start-code",
        default="VW0001",
        help="First dealer_code in the batch (default: VW0001).",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=REGISTRY_CSV,
        help="Path to data/dealer_master.csv (default: project registry).",
    )
    args = parser.parse_args(argv)

    urls = parse_urls_file(args.urls_file)
    if not urls:
        log.error("no_urls_parsed", file=str(args.urls_file))
        return 1

    inputs = _assign_dealer_codes(urls, args.start_code)
    log.info("run_start", count=len(inputs), start_code=args.start_code)

    results = asyncio.run(run_discovery(inputs))

    write_registry_csv(args.registry, results)

    success = sum(1 for r in results if r.error is None)
    log.info(
        "run_done",
        total=len(results),
        success=success,
        errors=len(results) - success,
    )
    return 0 if success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
