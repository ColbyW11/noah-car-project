"""Headless Playwright probe for live DOM inspection.

For each dealer URL below, navigates the page, waits for it to settle, then logs:
- final URL after redirects
- page title
- list of iframe srcs
- count of buttons / selects / inputs / "Schedule" links
- script src URLs that contain platform-relevant keywords
- any console errors

Usage: uv run python /tmp/dealer_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright, Browser, BrowserContext

PROBES = [
    {
        "label": "VW0001 Teddy schedule",
        "url": "https://www.teddyvolkswagen.com/ScheduleService",
        "wait_seconds": 12,
    },
    {
        "label": "VW0004 West Islip schedule",
        "url": "https://www.vwofwestislip.com/scheduleservice",
        "wait_seconds": 12,
    },
    {
        "label": "VW0002 Jeff schedule",
        "url": "https://www.gojeffvw.com/serviceappmt.aspx",
        "wait_seconds": 12,
    },
]

PLATFORM_HINTS = (
    "xtime",
    "consumer.xtime",
    "consumerschedulingfe",
    "mykaarma",
    "mk-scheduler",
    "connectcdk",
    "dealerfx",
    "dealer-fx",
    "tvi-mt",
    "tvi-",
    "servicedealer",
    "sonic",
    "kpa",
    "fixedopsdigital",
    "dealersocket",
    "asurint",
    "dms360",
    "schedule",
    "service",
    "appointment",
    "appt",
)


async def probe_one(browser: Browser, label: str, url: str, wait_seconds: int) -> dict:
    out: dict = {"label": label, "input_url": url}
    context: BrowserContext = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        viewport={"width": 1400, "height": 900},
    )
    console_messages: list[str] = []
    iframe_log: list[str] = []
    request_urls: list[str] = []

    page = await context.new_page()
    page.on("console", lambda msg: console_messages.append(f"{msg.type}: {msg.text[:200]}"))
    page.on("frameattached", lambda f: iframe_log.append(f"attached:{f.url[:200]}"))
    page.on("request", lambda r: request_urls.append(r.url))

    try:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            out["http_status"] = response.status if response else None
            out["final_url"] = page.url
        except Exception as exc:
            out["nav_error"] = repr(exc)[:200]
            await context.close()
            return out

        try:
            await page.wait_for_load_state("networkidle", timeout=wait_seconds * 1000)
        except Exception:
            pass

        # Settle pass
        await page.wait_for_timeout(2000)

        out["title"] = await page.title()

        # Frames
        frames = page.frames
        out["frame_count"] = len(frames)
        out["frame_urls"] = [f.url[:300] for f in frames if f.url and f.url != "about:blank"]

        # DOM probes
        out["dom"] = {
            "buttons": await page.locator("button").count(),
            "selects": await page.locator("select").count(),
            "inputs": await page.locator("input").count(),
            "iframes": await page.locator("iframe").count(),
            "links_schedule_text": await page.locator("a:has-text('Schedule'), button:has-text('Schedule')").count(),
            "oil_change_mentions": await page.locator("*:has-text('Oil Change')").count(),
        }

        # Iframe srcs
        iframe_srcs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src).filter(Boolean)"
        )
        out["iframe_srcs"] = iframe_srcs[:20]

        # Script srcs with platform hints
        scripts = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)"
        )
        hinted: list[str] = []
        for s in scripts:
            low = s.lower()
            if any(h in low for h in PLATFORM_HINTS):
                hinted.append(s)
        out["scripts_with_platform_hints"] = hinted[:25]
        out["total_scripts"] = len(scripts)

        # Hint requests
        hint_requests = []
        for req in request_urls[-300:]:
            low = req.lower()
            if any(h in low for h in ("xtime", "kaarma", "connectcdk", "dealer-fx", "dealerfx", "tvi", "sonic", "consumer.")):
                hint_requests.append(req[:300])
        out["platform_hint_requests"] = list(dict.fromkeys(hint_requests))[:15]

        # Console errors
        out["console_errors"] = [m for m in console_messages if m.startswith("error")][:10]

        # Body text snippet
        body_text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 800) : ''")
        out["body_text_first_800"] = body_text

    finally:
        await context.close()

    return out


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            results = []
            for p in PROBES:
                print(f"\n=== probing: {p['label']} ===", file=sys.stderr)
                res = await probe_one(browser, p["label"], p["url"], p["wait_seconds"])
                results.append(res)
                print(json.dumps(res, indent=2, default=str))
        finally:
            await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
