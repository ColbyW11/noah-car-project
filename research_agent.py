"""AI-powered VW dealer research agent.

Uses the Claude API with server-side web search to automatically research
VW dealers — finding scheduler platforms, service URLs, reviews, etc.

Usage:
    .venv/bin/python research_agent.py "Find the service scheduler URLs for all VW dealers in Minnesota"
    .venv/bin/python research_agent.py "What scheduling platform does each dealer in dealers.csv use?"
    .venv/bin/python research_agent.py "Research reviews and ratings for VW dealers in Texas"

Requires:
    uv pip install anthropic
    Environment variable: ANTHROPIC_API_KEY
"""

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


def run(prompt: str):
    client = anthropic.Anthropic()

    # Build the user message with dealer context
    dealers_context = load_dealers_context()
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


def main():
    if len(sys.argv) < 2:
        print("Usage: python research_agent.py \"<your research question>\"")
        print()
        print("Examples:")
        print('  python research_agent.py "Find service scheduler URLs for VW dealers in MN"')
        print('  python research_agent.py "Compare oil change prices at VW dealers in Texas"')
        print('  python research_agent.py "What platform does Autobahn VW Fort Worth use?"')
        print('  python research_agent.py "Find all VW dealers in Florida and their scheduler URLs"')
        sys.exit(1)

    run(sys.argv[1])


if __name__ == "__main__":
    main()
