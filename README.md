# VW Dealer Oil Change Availability Tool

Find the soonest available oil change appointment across VW dealerships. Two approaches:

1. **Scraper** (`main.py`) — Visits dealer service schedulers with a browser bot and collects the earliest appointment date/time into a spreadsheet.
2. **AI Research Agent** (`research_agent.py`) — Uses Claude with web search to research dealers, find scheduler URLs, identify platforms, and more.

## Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Setup

```bash
# Create venv and install dependencies
uv venv
uv pip install -r requirements.txt

# Install browser for the scraper
.venv/bin/playwright install chromium

# For the AI research agent, also install:
uv pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Scraper

Visits each dealer's online service scheduler, inputs a VIN, selects oil change, and returns the earliest available appointment.

### Supported platforms

- **Tekion** — hosted scheduler at `tekioncloud.com`
- **Xtime** — embedded iframe scheduler on dealer websites

### Usage

```bash
# Basic run (opens a visible browser)
.venv/bin/python main.py --dealers dealers.csv

# Filter by state
.venv/bin/python main.py --dealers dealers.csv --state MN

# Headless mode (no browser window)
.venv/bin/python main.py --dealers dealers.csv --headless

# Output to Excel
.venv/bin/python main.py --dealers dealers.csv --output results/output.xlsx --excel

# Use a specific VIN
.venv/bin/python main.py --dealers dealers.csv --vin 1VWSA7A32LC099999
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--dealers` | Path to dealers CSV file (required) | — |
| `--output` | Output file path | `results/output.csv` |
| `--state` | Filter by state abbreviation (e.g., `MN`) | all |
| `--vin` | VIN for scheduling lookup | placeholder VIN |
| `--headless` | Run browser without GUI | off |
| `--excel` | Output as `.xlsx` instead of CSV | off |

### Output columns

| Column | Example |
|--------|---------|
| Dealer Name | Luther Westside Volkswagen |
| State | MN |
| Platform | tekion |
| Earliest Date | March 15 |
| Earliest Time | 9:00 AM |
| Status | success / blocked / error |
| Error | (reason if not successful) |
| Screenshot Path | results/screenshots/Luther_Westside_20260304.png |
| URL | (scheduler URL) |

## AI Research Agent

Uses the Claude API with server-side web search to research VW dealers automatically — no browser needed.

```bash
.venv/bin/python research_agent.py "Find service scheduler URLs for VW dealers in MN"
.venv/bin/python research_agent.py "Compare oil change prices at VW dealers in Texas"
.venv/bin/python research_agent.py "What platform does Autobahn VW Fort Worth use?"
.venv/bin/python research_agent.py "Find all VW dealers in Florida and their scheduler URLs"
```

The agent automatically:
- Searches the web for dealer info
- Reads your existing `dealers.csv` for context
- Streams results to the terminal in real-time
- Cites sources with URLs

## Dealer CSV format

```
name,url,platform,state
Luther Westside Volkswagen,https://www.westsidevw.com/service/schedule-service/,tekion,MN
Schmelz Countryside Volkswagen,https://www.schmelzvw.com/service-schedule.html,xtime,MN
```

| Column | Description |
|--------|-------------|
| `name` | Dealer name |
| `url` | Direct URL to the dealer's service scheduler page |
| `platform` | `tekion` or `xtime` |
| `state` | Two-letter state code |

## Known limitations

- **Tekion sign-in**: Most Tekion schedulers require phone/email verification. Reported as "blocked" unless a guest option exists.
- **Captchas**: Pages with captcha challenges are flagged as "blocked".
- **Selector variability**: Dealer pages can differ in layout. Running in headed mode (default) helps debug.
- **Rate limiting**: 3-second delay between dealers. Adjust `REQUEST_DELAY` in `config.py` if needed.
