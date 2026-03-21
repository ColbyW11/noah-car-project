"""VW Dealer Oil Change Availability Scraper.

Reads VW dealers from a text file and uses OpenClaw to find the earliest
available oil change appointment at each dealer.

Usage:
    python scrape.py
    python scrape.py --dealers other_dealers.txt
    python scrape.py --output results/custom.xlsx
"""

import argparse
import os
import re
import json

import requests
import pandas as pd

from config import (
    OPENCLAW_GATEWAY,
    OPENCLAW_TOKEN,
    DEFAULT_VIN,
    DEALER_FILE,
    DEFAULT_OUTPUT,
)


def load_dealers(path):
    """Parse dealer file into a list of dicts with name, url, phone."""
    dealers = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Split on phone number pattern: (xxx) xxx-xxxx or xxx-xxx-xxxx
            match = re.match(
                r"^([\w.\-]+(?:\.[\w.\-]+)*)\s+((?:\(\d{3}\)\s*|\d{3}-)\d{3}-\d{4})$",
                line,
            )
            if not match:
                print(f"  Warning: could not parse line: {line}")
                continue
            raw_url = match.group(1).rstrip(".")
            phone = match.group(2).strip()
            # Derive a readable name from the domain
            name = raw_url.replace("www.", "").split(".")[0]
            dealers.append(
                {
                    "name": name,
                    "url": f"https://{raw_url}",
                    "phone": phone,
                }
            )
    return dealers


TASK_PROMPT = """\
Open the browser and navigate to {url}.

Find their service scheduler page (look for "Schedule Service", "Book Appointment",
or similar links). Then find the earliest available oil change appointment.

If the scheduler asks for a VIN, enter: {vin}
If it asks for vehicle info without a VIN field, use: Year: 2020, Make: Volkswagen, Model: Atlas.

Look for oil change under names like "Oil Change", "Lube, Oil & Filter",
"Express Service", "Maintenance", or similar.

If you encounter a login wall, captcha, or phone/email verification, stop and
report that you were blocked.

When done, respond with EXACTLY this format (one field per line):
STATUS: success OR blocked OR error
DATE: <earliest available date, e.g. March 15, 2026>
TIME: <earliest available time, e.g. 9:00 AM>
NOTES: <any additional context or error description>
"""


def send_to_openclaw(prompt, token):
    """Send a task to OpenClaw's chat completions API and return the response text."""
    url = f"{OPENCLAW_GATEWAY}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "model": "openclaw:main",
        "messages": [{"role": "user", "content": prompt}],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=300)
    resp.raise_for_status()

    data = resp.json()
    # OpenAI-compatible format: choices[0].message.content
    return data["choices"][0]["message"]["content"]


def parse_response(text):
    """Parse OpenClaw's structured response into a dict."""
    result = {"status": "error", "earliest_date": "", "earliest_time": "", "notes": ""}

    for line in text.split("\n"):
        line = line.strip()
        if line.upper().startswith("STATUS:"):
            result["status"] = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("DATE:"):
            result["earliest_date"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("TIME:"):
            result["earliest_time"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("NOTES:"):
            result["notes"] = line.split(":", 1)[1].strip()

    # If we couldn't parse a status, store the full response as notes
    if result["status"] not in ("success", "blocked", "error"):
        result["status"] = "error"
        result["notes"] = text[:500]

    return result


def scrape_dealers(dealers, vin, token):
    """Send each dealer to OpenClaw and collect results."""
    results = []

    for i, dealer in enumerate(dealers):
        print(f"[{i + 1}/{len(dealers)}] Scraping {dealer['name']} ({dealer['url']})...")

        prompt = TASK_PROMPT.format(url=dealer["url"], vin=vin)

        try:
            response_text = send_to_openclaw(prompt, token)
            parsed = parse_response(response_text)
        except requests.exceptions.ConnectionError:
            print("  -> ERROR: Could not connect to OpenClaw. Is the gateway running?")
            print("     Start it with: openclaw gateway start")
            parsed = {
                "status": "error",
                "earliest_date": "",
                "earliest_time": "",
                "notes": "Could not connect to OpenClaw gateway",
            }
        except Exception as e:
            print(f"  -> ERROR: {e}")
            parsed = {
                "status": "error",
                "earliest_date": "",
                "earliest_time": "",
                "notes": str(e),
            }

        result = {
            "Dealer": dealer["name"],
            "URL": dealer["url"],
            "Phone": dealer["phone"],
            "Earliest Date": parsed["earliest_date"],
            "Earliest Time": parsed["earliest_time"],
            "Status": parsed["status"],
            "Notes": parsed["notes"],
        }
        results.append(result)

        if parsed["status"] == "success":
            print(f"  -> {parsed['earliest_date']} {parsed['earliest_time']}")
        elif parsed["status"] == "blocked":
            print(f"  -> BLOCKED: {parsed['notes']}")
        else:
            print(f"  -> ERROR: {parsed['notes']}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Scrape VW dealer websites for oil change availability using OpenClaw"
    )
    parser.add_argument(
        "--dealers",
        default=DEALER_FILE,
        help=f"Path to dealer text file (default: {DEALER_FILE})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output Excel file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--vin",
        default=DEFAULT_VIN,
        help=f"VIN to use for scheduling (default: {DEFAULT_VIN})",
    )
    parser.add_argument(
        "--token",
        default=OPENCLAW_TOKEN,
        help="OpenClaw auth token (or set OPENCLAW_TOKEN env var)",
    )

    args = parser.parse_args()

    # Load dealers
    print(f"Loading dealers from {args.dealers}...")
    dealers = load_dealers(args.dealers)
    if not dealers:
        print("No dealers found. Check your dealer file.")
        return

    print(f"Found {len(dealers)} dealer(s)")
    print(f"Using VIN: {args.vin}")
    print(f"OpenClaw gateway: {OPENCLAW_GATEWAY}")
    print("-" * 60)

    # Scrape
    results = scrape_dealers(dealers, args.vin, args.token)

    # Write output
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    df = pd.DataFrame(results)
    df.to_excel(args.output, index=False)
    print("-" * 60)
    print(f"Results saved to {args.output}")

    # Summary
    success = sum(1 for r in results if r["Status"] == "success")
    blocked = sum(1 for r in results if r["Status"] == "blocked")
    failed = sum(1 for r in results if r["Status"] == "error")
    print(f"Summary: {success} succeeded, {failed} failed, {blocked} blocked")


if __name__ == "__main__":
    main()
