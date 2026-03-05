"""Dealer list loading and management."""

import csv


def load_dealers(csv_path, state_filter=None):
    """Load dealers from a CSV file.

    Args:
        csv_path: path to CSV with columns: name, url, platform, state
        state_filter: optional state abbreviation to filter by (e.g., "TX")

    Returns:
        list of dicts with dealer info
    """
    dealers = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize platform from URL if not specified
            if not row.get("platform"):
                row["platform"] = detect_platform(row.get("url", ""))

            # Normalize platform name
            row["platform"] = row["platform"].strip().lower()

            # Filter by state if specified
            if state_filter:
                if row.get("state", "").strip().upper() != state_filter.upper():
                    continue

            dealers.append(row)

    return dealers


def detect_platform(url):
    """Detect the scheduler platform from a URL."""
    url_lower = url.lower()
    if "tekioncloud.com" in url_lower or "tekion" in url_lower:
        return "tekion"
    # Default to xtime as it's the most common
    return "xtime"
