"""Configuration settings for the VW dealer oil change scraper."""

# Placeholder VIN (generic VW format)
DEFAULT_VIN = "1VWSA7A32LC011111"

# Delay between dealer requests (seconds)
REQUEST_DELAY = 3

# Browser timeout for page loads (milliseconds)
PAGE_TIMEOUT = 30000

# Timeout for individual actions like clicking/filling (milliseconds)
ACTION_TIMEOUT = 10000

# Default output file
DEFAULT_OUTPUT = "results/output.csv"

# Screenshots directory
SCREENSHOTS_DIR = "results/screenshots"

# Default headless mode
DEFAULT_HEADLESS = False
