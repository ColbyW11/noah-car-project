# VW Dealer Oil Change Finder

Automatically checks VW dealer websites and finds the soonest available oil change appointment. Results go into an Excel spreadsheet.

Uses [OpenClaw](https://openclaw.ai/) — an open-source AI agent that controls a browser, navigates each dealer's service scheduler, and reports back the earliest opening.

## What it does

For each dealer listed in `vwdealders_from_noah.txt`, the script:

1. Tells OpenClaw to open the dealer's website in a browser
2. OpenClaw finds the "Schedule Service" page on its own
3. It enters vehicle info (VIN or year/make/model), selects "Oil Change", and navigates through the scheduler
4. It reports back the earliest available date and time
5. Everything gets saved to `results/output.xlsx`

## Prerequisites

You need three things installed before running:

### 1. Python 3.13+

Check with `python --version`. Install from [python.org](https://python.org) if needed.

### 2. uv (Python package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 3. OpenClaw

OpenClaw is the AI agent that does the actual browser work. Install it via npm:

```bash
npm i -g openclaw
```

Then run the onboarding to configure it (sets up your LLM API key, browser, etc.):

```bash
openclaw onboard
```

This walks you through picking an LLM provider (Claude, GPT, etc.) and configuring browser access. Follow the prompts.

## Setup (first time only)

```bash
# Clone the repo and cd into it
git clone <repo-url>
cd noah-car-project

# Install Python dependencies
uv sync
```

## Running the scraper

### Step 1: Start OpenClaw

OpenClaw runs a local gateway server that the script talks to. Start it in a separate terminal:

```bash
openclaw gateway start
```

Leave this running. The gateway listens on `http://127.0.0.1:18789` by default.

### Step 2: Run the script

```bash
python scrape.py
```

That's it. The script reads the dealer list, sends each one to OpenClaw, and saves results to `results/output.xlsx`.

You'll see progress in the terminal:

```
Loading dealers from vwdealders_from_noah.txt...
Found 5 dealer(s)
Using VIN: 1VWSA7A32LC011111
OpenClaw gateway: http://127.0.0.1:18789
------------------------------------------------------------
[1/5] Scraping teddyvolkswagen (https://www.teddyvolkswagen.com)...
  -> March 22, 2026 9:00 AM
[2/5] Scraping gojeffvw (https://www.gojeffvw.com)...
  -> March 21, 2026 10:30 AM
...
```

### Options

```bash
# Use a different dealer file
python scrape.py --dealers my_dealers.txt

# Save output somewhere else
python scrape.py --output my_results.xlsx

# Use your own VIN (instead of the placeholder)
python scrape.py --vin 1VWSA7A32LC099999

# Pass an OpenClaw auth token directly
python scrape.py --token your-token-here
```

| Flag | What it does | Default |
|------|-------------|---------|
| `--dealers` | Path to the dealer text file | `vwdealders_from_noah.txt` |
| `--output` | Where to save the Excel file | `results/output.xlsx` |
| `--vin` | VIN to enter on scheduler pages | `1VWSA7A32LC011111` |
| `--token` | OpenClaw auth token | `OPENCLAW_TOKEN` env var |

## Configuration

Settings live in `config.py`. You can edit them directly or override via environment variables:

| Setting | Env variable | Default | What it controls |
|---------|-------------|---------|-----------------|
| `OPENCLAW_GATEWAY` | `OPENCLAW_GATEWAY` | `http://127.0.0.1:18789` | Where the OpenClaw gateway is running |
| `OPENCLAW_TOKEN` | `OPENCLAW_TOKEN` | (empty) | Auth token for the gateway |
| `DEFAULT_VIN` | — | `1VWSA7A32LC011111` | Placeholder VIN used on scheduler pages |
| `DEALER_FILE` | — | `vwdealders_from_noah.txt` | Default dealer list file |
| `DEFAULT_OUTPUT` | — | `results/output.xlsx` | Default output path |

### OpenClaw auth token

If your OpenClaw gateway has authentication enabled (it does by default after onboarding), you need to provide the token. Two ways:

```bash
# Option 1: environment variable (recommended)
export OPENCLAW_TOKEN="your-token-here"
python scrape.py

# Option 2: command line flag
python scrape.py --token your-token-here
```

To find your token, check your OpenClaw config:

```bash
cat ~/.openclaw/openclaw.json | grep token
```

### Changing the gateway URL

If OpenClaw is running on a different port or machine:

```bash
export OPENCLAW_GATEWAY="http://192.168.1.50:18789"
python scrape.py
```

## Dealer file format

The file `vwdealders_from_noah.txt` has one dealer per line — a website followed by a phone number:

```
www.teddyvolkswagen.com (718) 920-1400
www.gojeffvw.com 610-873-2400
piazzavw.com (610) 896-4853
vwofwestislip.com.  (631) 650-3400
vwnanuet.com.  845-285-3400
```

To add more dealers, just add more lines in the same format. The script handles:
- URLs with or without `www.`
- Trailing periods (e.g., `vwnanuet.com.` is cleaned up automatically)
- Phone numbers in `(xxx) xxx-xxxx` or `xxx-xxx-xxxx` format

## Output

Results are saved to an Excel file (`results/output.xlsx` by default) with these columns:

| Column | Description | Example |
|--------|-------------|---------|
| Dealer | Name derived from the website | teddyvolkswagen |
| URL | Full URL the agent visited | https://www.teddyvolkswagen.com |
| Phone | Dealer phone number | (718) 920-1400 |
| Earliest Date | Soonest oil change date found | March 22, 2026 |
| Earliest Time | Time slot for that date | 9:00 AM |
| Status | `success`, `blocked`, or `error` | success |
| Notes | Extra context (especially for non-success) | Captcha required |

### Status meanings

- **success** — found an available appointment
- **blocked** — hit a login wall, captcha, or verification step that couldn't be bypassed
- **error** — something went wrong (connection issue, page didn't load, etc.)

## Troubleshooting

**"Could not connect to OpenClaw. Is the gateway running?"**
Start the gateway: `openclaw gateway start`

**401 Unauthorized errors**
Your auth token is missing or wrong. Set it with `export OPENCLAW_TOKEN="..."` or pass `--token`.

**OpenClaw isn't finding the scheduler**
Some dealer websites have unusual layouts. OpenClaw does its best but may not navigate every site successfully. These show up as `error` status in the output.

**Slow results**
Each dealer takes 1-3 minutes depending on how complex the scheduler is. With 5 dealers, expect ~5-15 minutes total.

## Project structure

```
noah-car-project/
  scrape.py                  # Main script — run this
  config.py                  # Settings (gateway URL, VIN, paths)
  vwdealders_from_noah.txt   # List of VW dealers to check
  results/
    output.xlsx              # Output spreadsheet (created after running)
  pyproject.toml             # Python project config / dependencies
  requirements.txt           # Dependencies (for pip users)
```
