"""Output writer for scrape results."""

import csv
import os

import pandas as pd


COLUMNS = [
    "dealer_name",
    "state",
    "platform",
    "earliest_date",
    "earliest_time",
    "status",
    "error",
    "screenshot_path",
    "url",
]


def write_csv(results, output_path):
    """Write results to a CSV file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow({col: result.get(col, "") for col in COLUMNS})

    print(f"Results saved to {output_path}")


def write_excel(results, output_path):
    """Write results to an Excel file with formatting."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    df = pd.DataFrame(results, columns=COLUMNS)
    df.columns = [
        "Dealer Name",
        "State",
        "Platform",
        "Earliest Date",
        "Earliest Time",
        "Status",
        "Error",
        "Screenshot Path",
        "URL",
    ]

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Results")

    print(f"Results saved to {output_path}")
