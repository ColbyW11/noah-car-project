"""AI-powered VW dealer research agent.

Uses the Claude API with server-side web search to automatically research
VW dealers — finding scheduler platforms, service URLs, reviews, etc.

Usage:
    python research_agent.py --location "Minnesota"
    python research_agent.py --location "Texas" --pricing
    python research_agent.py --dealer "Schmelz Countryside Volkswagen"

Requires:
    uv pip install anthropic
    Environment variable: ANTHROPIC_API_KEY
"""

import argparse
import csv
import sys

import anthropic


SYSTEM_PROMPT = """\
You are a VW dealer research assistant. Your job is to help find information
about Volkswagen dealerships across the US, focusing on:

- Service scheduler URLs and platforms (Xtime, Tekion, DealerFX, etc.)
- Dealer contact info, locations, and hours
- Service department reviews and ratings
- Oil change pricing and availability patterns

When researching dealers:
1. Search the web for the dealer's service scheduling page
2. Identify which platform they use (look for xtime, tekion, dealerfx in URLs)
3. Report findings in a clear, structured format

Always cite your sources with URLs.\
"""


def load_dealers_context(path="dealers.csv"):
    """Load dealers.csv and return as context string."""
    try:
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            return ""
        lines = ["Current dealers.csv contents:"]
        for row in rows:
            lines.append(f"  - {row['name']} | {row['state']} | {row['platform']} | {row['url']}")
        return "\n".join(lines)
    except FileNotFoundError:
        return ""


def run(prompt: str, dealers_path: str = "dealers.csv"):
    client = anthropic.Anthropic()

    # Build the user message with dealer context
    dealers_context = load_dealers_context(dealers_path)
    full_prompt = prompt
    if dealers_context:
        full_prompt = f"{dealers_context}\n\n---\n\nResearch request: {prompt}"

    messages = [{"role": "user", "content": full_prompt}]

    # Server-side web search + fetch — Claude handles all searching automatically
    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]

    print(f"Researching: {prompt}")
    print("-" * 60)

    # Agentic loop — keeps going until Claude is done searching
    max_continuations = 10
    for _ in range(max_continuations):
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        print(event.delta.text, end="", flush=True)

            response = stream.get_final_message()

        if response.stop_reason == "end_turn":
            break

        # Server-side tool hit iteration limit — continue automatically
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": full_prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue

        break

    print()
    print("-" * 60)
    print(f"Tokens used: {response.usage.input_tokens} in / {response.usage.output_tokens} out")


def build_prompt(args):
    """Build a research prompt from structured CLI arguments."""
    if args.dealer:
        return (
            f"Research {args.dealer} dealership. Find their service scheduler URL, "
            f"scheduling platform (Xtime, Tekion, DealerFX, etc.), oil change pricing, "
            f"hours, address, and contact info."
        )

    # --location mode
    prompt = (
        f"Find all Volkswagen dealerships in {args.location}. "
        f"For each dealer, find their service scheduler URL and identify which "
        f"scheduling platform they use (Xtime, Tekion, DealerFX, etc.). "
        f"Include addresses and phone numbers."
    )
    if args.pricing:
        prompt += (
            f" Also compare oil change pricing across these dealers."
        )
    return prompt


def main():
    parser = argparse.ArgumentParser(
        description="AI-powered VW dealer research agent"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--location",
        help="Search for VW dealers in a location (e.g., 'Texas', 'Minneapolis, MN')",
    )
    group.add_argument(
        "--dealer",
        help="Research a specific dealer by name (e.g., 'Schmelz Countryside Volkswagen')",
    )
    parser.add_argument(
        "--pricing",
        action="store_true",
        help="Include oil change pricing comparison (use with --location)",
    )
    parser.add_argument(
        "--dealers",
        default="dealers.csv",
        help="Path to dealers CSV for context (default: dealers.csv)",
    )

    args = parser.parse_args()
    prompt = build_prompt(args)
    run(prompt, dealers_path=args.dealers)


if __name__ == "__main__":
    main()
