# VW Dealer Oil Change Availability Tool

Find the soonest available oil change appointment across VW dealerships.

## Workflow

1. **Research** — Use AI to find dealers, scheduler URLs, and platforms
2. **Add dealers** — Add results to `dealers.csv`
3. **Scrape** — AI agent navigates dealer websites to check appointment availability

```bash
python cli.py research --location "Minnesota"
# (add dealers to dealers.csv)
python cli.py scrape
```

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
# Install dependencies
uv sync

# Install browser for the scraper
uv run playwright install chromium

# Set your API key (required for both research and scrape commands)
export ANTHROPIC_API_KEY="sk-ant-..."
```

## CLI Usage

All commands are accessed through `cli.py`:

```bash
python cli.py --help
python cli.py scrape --help
python cli.py research --help
```

### Research — find dealers with AI

Uses Claude with web search to find VW dealers, scheduler URLs, platforms, and pricing. No browser needed.

```bash
# Find all VW dealers in a location
python cli.py research --location "Minnesota"
python cli.py research --location "Dallas, TX"

# Include oil change pricing comparison
python cli.py research --location "Texas" --pricing

# Research a specific dealer
python cli.py research --dealer "Schmelz Countryside Volkswagen"
```

| Flag | Description |
|------|-------------|
| `--location` | Search for dealers in a location (e.g., `Texas`, `Minneapolis, MN`) |
| `--dealer` | Research a specific dealer by name |
| `--pricing` | Include oil change pricing comparison (use with `--location`) |
| `--dealers` | Path to dealers CSV for context (default: `dealers.csv`) |

One of `--location` or `--dealer` is required.

### Scrape — check appointment availability

Uses a Claude AI agent to navigate each dealer's online service scheduler. The agent sees the page via screenshots, fills in the VIN, selects oil change, and finds the earliest available appointment — no hardcoded selectors or platform-specific logic needed.

```bash
# Scrape all dealers in dealers.csv
python cli.py scrape

# Filter by state
python cli.py scrape --state MN

# Headless mode (no browser window)
python cli.py scrape --headless

# Output to Excel
python cli.py scrape --output results/output.xlsx --excel

# Use a specific VIN
python cli.py scrape --vin 1VWSA7A32LC099999

# Use a cheaper/faster model
python cli.py scrape --model claude-haiku-4-5
```

| Flag | Description | Default |
|------|-------------|---------|
| `--dealers` | Path to dealers CSV file | `dealers.csv` |
| `--output` | Output file path | `results/output.csv` |
| `--state` | Filter by state abbreviation (e.g., `MN`) | all |
| `--vin` | VIN for scheduling lookup | placeholder VIN |
| `--headless` | Run browser without GUI | off |
| `--excel` | Output as `.xlsx` instead of CSV | off |
| `--model` | Claude model to use | `claude-sonnet-4-6` |

#### How the AI agent works

For each dealer, the agent:
1. Opens the scheduler URL in a Playwright browser
2. Takes a screenshot and sends it to Claude
3. Claude decides what to do next (click, fill a field, scroll, etc.)
4. The action is executed via Playwright, and a new screenshot is taken
5. This loops until Claude finds the earliest appointment or reports a blocker

The agent works with **any** scheduler platform — no platform-specific code required.

#### Output columns

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
| `platform` | Scheduler platform (informational only — the AI agent handles any platform) |
| `state` | Two-letter state code |

## Configuration

Agent settings can be adjusted in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `AGENT_MODEL` | `claude-sonnet-4-6` | Claude model for the scraping agent |
| `AGENT_MAX_TURNS` | `25` | Max agentic loop iterations per dealer |
| `SCREENSHOT_WIDTH` | `1024` | Screenshot resize width (smaller = cheaper API calls) |
| `SCREENSHOT_HEIGHT` | `768` | Screenshot resize height |
| `REQUEST_DELAY` | `3` | Seconds between dealer requests |

## Known limitations

- **Sign-in walls**: Some schedulers require phone/email verification. The agent reports these as "blocked".
- **Captchas**: Pages with captcha challenges are reported as "blocked".
- **API cost**: Each dealer uses ~15-25 API turns with screenshots. Expect ~$0.10-0.30 per dealer with Sonnet, less with Haiku.
- **Speed**: Each dealer takes ~60-100 seconds as the agent navigates the site.
