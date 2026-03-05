"""VW Dealer Tool — unified CLI.

Workflow:
    1. Research dealers:  python cli.py research --location "Minnesota"
    2. Add results to dealers.csv
    3. Scrape availability: python cli.py scrape

Commands:
    scrape    Scrape dealer websites for the earliest oil change appointment
    research  Use AI to find dealers, scheduler URLs, and pricing
"""

import argparse
import asyncio
import os
import sys


def cmd_scrape(args):
    """Run the AI-powered scraper."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)
    from main import run
    asyncio.run(run(args))


def cmd_research(args):
    """Run the AI research agent."""
    from research_agent import build_prompt, run
    prompt = build_prompt(args)
    run(prompt, dealers_path=args.dealers)


def main():
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description=(
            "VW Dealer Tool — find oil change availability across VW dealerships.\n\n"
            "Workflow:\n"
            "  1. research  — Find dealers and their scheduler URLs using AI\n"
            "  2. (add results to dealers.csv)\n"
            "  3. scrape    — Check appointment availability on dealer websites"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- scrape ---
    sp_scrape = subparsers.add_parser(
        "scrape",
        help="Scrape dealer websites for oil change availability",
        description="Launch a browser and scrape each dealer's service scheduler for the earliest oil change appointment.",
    )
    sp_scrape.add_argument(
        "--dealers", default="dealers.csv",
        help="Path to dealers CSV file (default: dealers.csv)",
    )
    sp_scrape.add_argument(
        "--output", default="results/output.csv",
        help="Output file path (default: results/output.csv)",
    )
    sp_scrape.add_argument(
        "--state",
        help="Filter dealers by state abbreviation (e.g., MN, TX)",
    )
    sp_scrape.add_argument(
        "--vin", default="1VWSA7A32LC011111",
        help="VIN to use for scheduling (default: placeholder VW VIN)",
    )
    sp_scrape.add_argument(
        "--headless", action="store_true", default=False,
        help="Run browser in headless mode",
    )
    sp_scrape.add_argument(
        "--excel", action="store_true",
        help="Output as Excel (.xlsx) instead of CSV",
    )
    sp_scrape.add_argument(
        "--model", default=None,
        help="Claude model to use (default: claude-sonnet-4-6, option: claude-haiku-4-5)",
    )
    sp_scrape.set_defaults(func=cmd_scrape)

    # --- research ---
    sp_research = subparsers.add_parser(
        "research",
        help="Use AI to research dealers, URLs, and pricing",
        description="Use Claude AI with web search to find VW dealers, their scheduler URLs, platforms, and pricing.",
    )
    research_group = sp_research.add_mutually_exclusive_group(required=True)
    research_group.add_argument(
        "--location",
        help="Search for VW dealers in a location (e.g., 'Texas', 'Minneapolis, MN')",
    )
    research_group.add_argument(
        "--dealer",
        help="Research a specific dealer by name (e.g., 'Schmelz Countryside Volkswagen')",
    )
    sp_research.add_argument(
        "--pricing", action="store_true",
        help="Include oil change pricing comparison (use with --location)",
    )
    sp_research.add_argument(
        "--dealers", default="dealers.csv",
        help="Path to dealers CSV for context (default: dealers.csv)",
    )
    sp_research.set_defaults(func=cmd_research)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
