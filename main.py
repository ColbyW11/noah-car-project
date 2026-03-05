"""VW Dealer Oil Change Availability Scraper.

Visits VW dealer service schedulers and finds the earliest available
oil change appointment. Results are saved to CSV or Excel.

Usage:
    python main.py --dealers dealers.csv --output results/output.csv
    python main.py --dealers dealers.csv --state TX --headless
    python main.py --dealers dealers.csv --output results/output.xlsx --excel
"""

import argparse
import asyncio
import time

from playwright.async_api import async_playwright

from config import DEFAULT_VIN, DEFAULT_OUTPUT, DEFAULT_HEADLESS, REQUEST_DELAY
from dealers import load_dealers
from output import write_csv, write_excel
from scrapers.tekion import TekionScraper
from scrapers.xtime import XtimeScraper


async def scrape_dealer(browser, dealer, vin, headless):
    """Scrape a single dealer's scheduler."""
    page = await browser.new_page()
    try:
        platform = dealer.get("platform", "xtime").lower()
        if platform == "tekion":
            scraper = TekionScraper(page, vin, headless)
        else:
            scraper = XtimeScraper(page, vin, headless)

        result = await scraper.scrape(dealer)
        return result
    finally:
        await page.close()


async def run(args):
    """Main scraping run."""
    # Load dealers
    dealers = load_dealers(args.dealers, state_filter=args.state)
    if not dealers:
        print("No dealers found. Check your CSV file and filters.")
        return

    print(f"Found {len(dealers)} dealer(s) to scrape")
    print(f"Using VIN: {args.vin}")
    print(f"Headless mode: {args.headless}")
    print("-" * 60)

    results = []
    success = 0
    failed = 0
    blocked = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)

        for i, dealer in enumerate(dealers):
            print(f"[{i+1}/{len(dealers)}] Scraping {dealer['name']}...")

            result = await scrape_dealer(browser, dealer, args.vin, args.headless)
            results.append(result)

            if result["status"] == "success":
                success += 1
                print(
                    f"  -> {result['earliest_date']} {result['earliest_time']}"
                )
            elif result["status"] == "blocked":
                blocked += 1
                print(f"  -> BLOCKED: {result['error']}")
            else:
                failed += 1
                print(f"  -> ERROR: {result['error']}")

            # Delay between requests
            if i < len(dealers) - 1:
                time.sleep(REQUEST_DELAY)

        await browser.close()

    # Write results
    print("-" * 60)
    output_path = args.output
    if args.excel or output_path.endswith(".xlsx"):
        if not output_path.endswith(".xlsx"):
            output_path = output_path.rsplit(".", 1)[0] + ".xlsx"
        write_excel(results, output_path)
    else:
        write_csv(results, output_path)

    # Summary
    print(f"\nSummary: {success} succeeded, {failed} failed, {blocked} blocked")
    print(f"Total: {len(dealers)} dealers processed")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape VW dealer service schedulers for oil change availability"
    )
    parser.add_argument(
        "--dealers",
        default="dealers.csv",
        help="Path to dealers CSV file (default: dealers.csv)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--state",
        help="Filter dealers by state abbreviation (e.g., TX)",
    )
    parser.add_argument(
        "--vin",
        default=DEFAULT_VIN,
        help=f"VIN to use for scheduling (default: {DEFAULT_VIN})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=DEFAULT_HEADLESS,
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--excel",
        action="store_true",
        help="Output as Excel (.xlsx) instead of CSV",
    )

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
