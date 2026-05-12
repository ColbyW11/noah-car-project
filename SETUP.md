# SETUP — outstanding work before the daily pipeline produces real data

The pipeline scaffolding (slices 0–9) is complete and tested, but two distinct
gaps must be closed before the `daily-scrape` workflow can actually accumulate
observations. The cron trigger in
[`.github/workflows/daily-scrape.yml`](.github/workflows/daily-scrape.yml) is
**commented out** until both gaps are resolved — otherwise we'd get a daily
stream of failing runs and (eventually) noisy alerts.

Re-enable the cron only after Section A is configured **and** Section B's
known scraper issues are addressed.

---

## A. Credentials & cloud setup

These are external/manual actions — no code change can substitute.

### 1. Google Cloud service account

Used by [`src/vw_scraper/storage/drive.py`](src/vw_scraper/storage/drive.py)
to authenticate Drive API calls.

- [ ] Create or reuse a GCP project.
- [ ] Enable the **Google Drive API** for that project.
- [ ] Create a **service account** (IAM & Admin → Service Accounts → Create).
- [ ] Generate a **JSON key** for the service account and download it.
- [ ] Save locally:
  ```bash
  mkdir -p ~/.config/vw-scraper
  mv ~/Downloads/<key>.json ~/.config/vw-scraper/service_account.json
  chmod 600 ~/.config/vw-scraper/service_account.json
  ```

### 2. Google Drive target folder

- [ ] Create a Drive folder for outputs (e.g. `vw-oil-availability/`).
- [ ] Share the folder with the service account email (`...iam.gserviceaccount.com`)
      as **Editor**.
- [ ] Copy the folder ID from its URL (`drive.google.com/drive/folders/<ID>`).

### 3. Local env

- [ ] `cp .env.example .env`
- [ ] Set `VW_SCRAPER_SA_PATH=~/.config/vw-scraper/service_account.json`
- [ ] Set `VW_SCRAPER_DRIVE_FOLDER_ID=<the folder ID>`
- [ ] (Optional) `VW_SCRAPER_SLACK_WEBHOOK=<incoming-webhook URL>` for alerts.

Smoke test the Drive sync without running the full pipeline:
```bash
uv run python -c "from vw_scraper.storage.drive import build_drive_service; import os; build_drive_service(os.path.expanduser(os.environ['VW_SCRAPER_SA_PATH']))"
```

### 4. Slack webhook (optional)

Skip if you don't want alerts. Otherwise:

- [ ] Slack → Apps → *Incoming Webhooks* → choose a channel → copy the webhook URL.
- [ ] Set `VW_SCRAPER_SLACK_WEBHOOK` locally and as a GitHub secret.

### 5. GitHub repo secrets

```bash
# Paste the JSON body — printf preserves newlines.
gh secret set GCP_SERVICE_ACCOUNT_JSON < ~/.config/vw-scraper/service_account.json

gh secret set VW_SCRAPER_DRIVE_FOLDER_ID --body "<the folder ID>"

# Optional — only if you set up Slack above.
gh secret set VW_SCRAPER_SLACK_WEBHOOK --body "<webhook URL>"
```

Verify: `gh secret list` should show all three.

### 6. Re-enable the scheduled cron

Once everything above is set and a manual `gh workflow run daily-scrape.yml`
succeeds:

- [ ] Uncomment the `schedule:` block in
      [`.github/workflows/daily-scrape.yml`](.github/workflows/daily-scrape.yml)
      (currently commented out, top of file).
- [ ] Commit and merge to `main`.
- [ ] The first scheduled run fires next 13:00 UTC.

---

## B. Live scraper status (snapshot as of 2026-05-12)

A local smoke run of `scripts/run_daily.py` on slice/09 produced **0 / 5 successful
observations**. The pipeline shape is correct — every dealer returns a valid
`ScrapeResult` with a structured error — but no dealer currently yields slot
data. Each item below is a real piece of work.

### B1. xtime widget no longer renders — VW0001, VW0004

**Status:** broken.

The dealer schedule page (e.g. `https://www.teddyvolkswagen.com/ScheduleService`)
loads, the cookie banner clicks through, the inline JS config for the xtime
scheduler is present in the page source — but the widget itself never renders.
DOM probe after cookie + 8s settle: 0 selects, 0 inputs, 0 "Schedule" links, no
xtime iframe. The page is effectively blank below the accessibility widget.

The walker in [`src/vw_scraper/scrapers/xtime.py`](src/vw_scraper/scrapers/xtime.py)
then spends ~52s of its 60s budget timing out on selectors that aren't there,
leaving 3–9s for the slot wait — which also times out because no XHR fires.

**To fix:** open VW0001 in headed Playwright
(`uv run python scripts/scrape_one.py VW0001 --headed`), drive the new flow by
hand, capture the actual interaction sequence (is there now a "Schedule"
button to click first? a vehicle selector flow? an iframe transition?), update
the walker selectors, re-capture fixtures, get the parser tests green against
the new HTML. Plan this as its own slice; reuse the existing diagnostic probe
pattern at `/tmp/xtime_probe.py` if helpful.

### B2. Unknown platforms — VW0002, VW0003

**Status:** never identified.

[`data/dealer_master.csv`](data/dealer_master.csv) lists VW0002 and VW0003 with
`platform=unknown`. Slice 2's discovery never produced a positive
identification, so the orchestrator skips them with
`UNEXPECTED: no scraper for platform=unknown`.

**To fix:** re-run platform discovery against these two dealer URLs. Either
update the registry with the correct platform value (and confirm a scraper
exists), or remove them from the active set if they use a platform we don't
yet support.

### B3. ConnectCDK live scrape not implemented — VW0005

**Status:** parser exists, live navigator stub.

Slice 8 delivered the ConnectCDK **parser** with fixture-based tests, but the
live `scrape()` method in [`src/vw_scraper/scrapers/connect_cdk.py`](src/vw_scraper/scrapers/connect_cdk.py)
returns `UNEXPECTED: live scrape not yet implemented for connect_cdk`. This is
the equivalent of Slice 4 for the second platform — never completed.

**To fix:** wire the navigation/form-fill logic the same way Slice 4 did for
xtime, instrument `scheduling_flow_seconds`, and add a `@pytest.mark.live`
test against VW0005.

---

## Suggested order of operations

1. **B1 (xtime)** first — it's the only platform where we've already proven a
   working end-to-end path, and it's two of the five pilot dealers. Fixing
   xtime unblocks 40% of the pilot.
2. **A1–A6 (credentials)** next — once at least one dealer is producing real
   data locally, Drive sync gives that data a home.
3. **B2 and B3** as follow-ups — they expand coverage but aren't blocking the
   "any data at all" milestone.
4. **Slice 10 (analytics notebook)** unblocked once enough days of real data
   exist (a week or two minimum to make the time series interesting).
