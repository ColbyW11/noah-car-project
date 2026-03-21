"""Configuration settings for the VW dealer oil change scraper."""

import os

# OpenClaw gateway settings
OPENCLAW_GATEWAY = os.getenv("OPENCLAW_GATEWAY", "http://127.0.0.1:18789")
OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN", "")

# Placeholder VIN (generic VW format)
DEFAULT_VIN = "1VWSA7A32LC011111"

# Default dealer file and output
DEALER_FILE = "vwdealders_from_noah.txt"
DEFAULT_OUTPUT = "results/output.xlsx"
