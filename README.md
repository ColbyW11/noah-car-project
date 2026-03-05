# VW Dealer Oil Change Availability Tool

Find the soonest available oil change appointment across VW dealerships.

## Workflow

1. **Research** — Use AI to find dealers, scheduler URLs, and platforms
2. **Add dealers** — Add results to `dealers.csv`
3. **Scrape** — Check appointment availability on dealer websites

```bash
python cli.py research --location "Minnesota"
# (add dealers to dealers.csv)
python cli.py scrape
```

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

# For the AI research agent, set your API key:
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

Visits each dealer's online service scheduler with a browser, inputs a VIN, selects oil change, and returns the earliest available appointment.

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
```

| Flag | Description | Default |
|------|-------------|---------|
| `--dealers` | Path to dealers CSV file | `dealers.csv` |
| `--output` | Output file path | `results/output.csv` |
| `--state` | Filter by state abbreviation (e.g., `MN`) | all |
| `--vin` | VIN for scheduling lookup | placeholder VIN |
| `--headless` | Run browser without GUI | off |
| `--excel` | Output as `.xlsx` instead of CSV | off |

#### Supported platforms

- **Tekion** — hosted scheduler at `tekioncloud.com`
- **Xtime** — embedded iframe scheduler on dealer websites

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
| `platform` | `tekion` or `xtime` |
| `state` | Two-letter state code |

## Known limitations

- **Tekion sign-in**: Most Tekion schedulers require phone/email verification. Reported as "blocked" unless a guest option exists.
- **Captchas**: Pages with captcha challenges are flagged as "blocked".
- **Selector variability**: Dealer pages can differ in layout. Running in headed mode (default) helps debug.
- **Rate limiting**: 3-second delay between dealers. Adjust `REQUEST_DELAY` in `config.py` if needed.
