# SETUP — optional cloud/Drive configuration

For current pipeline status (which dealers work, what action items
remain), see [`STATUS.md`](STATUS.md).

The local pipeline runs without any of the cloud setup below. JSONL +
Parquet land in `data/raw/<date>/` and `data/processed/timeseries.parquet`
on this machine. The launchd job in
`~/Library/LaunchAgents/com.colby.vw-scraper.daily.plist` fires daily
at 9 AM.

The Google Drive + GitHub Actions wiring described here is **optional**.
You only need it if you want the pipeline to:

- mirror data to a Drive folder (so it survives a disk failure or is
  shareable across machines), or
- run on GitHub Actions instead of (or in addition to) the local
  launchd job.

Until either of those is a goal, you can ignore this file.

---

## A. Google Drive service-account credentials

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

## B. Google Drive target folder

- [ ] Create a Drive folder for outputs (e.g. `vw-oil-availability/`).
- [ ] Share the folder with the service account email
      (`...iam.gserviceaccount.com`) as **Editor**.
- [ ] Copy the folder ID from its URL
      (`drive.google.com/drive/folders/<ID>`).

## C. Local environment

- [ ] `cp .env.example .env`
- [ ] Set `VW_SCRAPER_SA_PATH=~/.config/vw-scraper/service_account.json`
- [ ] Set `VW_SCRAPER_DRIVE_FOLDER_ID=<the folder ID>`
- [ ] (Optional) `VW_SCRAPER_SLACK_WEBHOOK=<incoming-webhook URL>` for
      additional alerts on top of the local macOS notification banner.

Smoke test the Drive sync without running the full pipeline:

```bash
uv run python -c "from vw_scraper.storage.drive import build_drive_service; \
  import os; \
  build_drive_service(os.path.expanduser(os.environ['VW_SCRAPER_SA_PATH']))"
```

## D. GitHub Actions cron (only if running off-laptop)

The workflow at `.github/workflows/daily-scrape.yml` has its
`schedule:` block commented out. Once the credentials above are
configured locally, add them as GitHub repo secrets:

```bash
gh secret set GCP_SERVICE_ACCOUNT_JSON < ~/.config/vw-scraper/service_account.json
gh secret set VW_SCRAPER_DRIVE_FOLDER_ID --body "<the folder ID>"
gh secret set VW_SCRAPER_SLACK_WEBHOOK --body "<webhook URL>"  # optional
```

Verify: `gh secret list` should show all three.

Then uncomment the `schedule:` block in
`.github/workflows/daily-scrape.yml`, commit, and merge. The first
scheduled run fires next at 13:00 UTC.

Note that headed-mode dealers (currently none, but watch out as you
add more dealers) won't run cleanly on GitHub Actions — the runner is
headless. The local launchd job is the right place for any dealer that
requires `--headed`.
