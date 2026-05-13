# Project Status — 2026-05-13

## Where the data lives

Each daily run writes to two places:

- **Raw, per-day**: `data/raw/<YYYY-MM-DD>/`
  - `observations.jsonl` — one JSON line per dealer scrape (success or
    structured error), schema in [`SPEC.md`](SPEC.md). Atomic write +
    idempotent re-run replaces the date's partition.
  - `run_metadata.json` — run-level summary (start/end time, success and
    error counts, scraper version).
- **Processed, append-only**: `data/processed/timeseries.parquet`
  — every successful observation across all dates, flattened (no
  per-slot detail; that lives in the raw JSONL). Same-day re-runs
  replace that date's rows, never duplicate.

Both paths are local-only. Google Drive sync is plumbed
(`src/vw_scraper/storage/drive.py`, `scripts/sync_drive.py`) but the
service-account credentials in [`SETUP.md`](SETUP.md) §A have not been
configured yet, so syncing to Drive is a no-op until those are set up.

## How it runs

Scheduled via macOS launchd at **9:00 AM local time daily**:

- Plist: `~/Library/LaunchAgents/com.colby.vw-scraper.daily.plist`
- Logs: `~/Library/Logs/vw-scraper.out.log` (JSON summary) and
  `~/Library/Logs/vw-scraper.err.log` (structlog progress + errors).
- Status: `launchctl print gui/$UID/com.colby.vw-scraper.daily`
- Manual fire (run now): `launchctl kickstart -k gui/$UID/com.colby.vw-scraper.daily`
- Unload (stop scheduling): `launchctl bootout gui/$UID/com.colby.vw-scraper.daily`
- Reload after editing plist: `launchctl bootout gui/$UID/...; launchctl bootstrap gui/$UID/<path>`

If the laptop is asleep at 9 AM, launchd fires the job at next wake.
Network failures, dealer-side outages, etc. don't break the schedule —
the next day's run is independent.

## Running manually

```bash
uv run python scripts/run_daily.py            # all active dealers → data/raw + data/processed
uv run python scripts/scrape_one.py VW0002    # single dealer → stdout
uv run python scripts/run_daily.py --headed   # show the browser (debug only)
uv run python scripts/run_daily.py --skip-timeseries  # raw JSONL only
```

## Dealer status

| Code   | Dealer             | Status              | Slots/day | Notes                                                                                                                            |
| ------ | ------------------ | ------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------- |
| VW0001 | Teddy VW           | **inactive**        | —         | Dealer disabled online scheduling on their site (`enableScheduleServiceButtons = false`). Phone-only via (718) 920-1400.         |
| VW0002 | Jeff D'Ambrosio VW | ✅ **working**      | ~135      | consumer.xtime.com modern SPA, webKey `vw20120702001037406497`.                                                                  |
| VW0003 | Piazza VW          | **inactive**        | —         | Host unreachable (port 443 ECONNREFUSED). Redirect target `piazzaautogroup.com` is Akamai-bot-blocked. Possibly defunct dealer.  |
| VW0004 | VW of West Islip   | ✅ **working**      | ~215      | TeamVelocity inline form on dealer.com, `xtime.teamvelocityportal.com` backend.                                                  |
| VW0005 | VW of Nanuet       | ⚠️ headless-blocked | 0         | ConnectCDK live nav reaches 8 of ~12 steps, then the React `/select-services` page never mounts in headless. Works headed only.  |

## Fixing the missing dealers

**VW0001 Teddy** — Nothing to scrape until the dealer re-enables online
scheduling. Their `/ScheduleService` page intentionally renders empty for
all visitors. If they ever turn it back on, the `secureoffersites.com`
Vue3 SPA will need its own scraper (it's neither Xtime nor ConnectCDK).
**Action**: monitor — re-check once a quarter by curling the page and
grepping for `enableScheduleServiceButtons = '...'`. No code change
needed today.

**VW0003 Piazza** — DNS resolves to `64.70.56.99` but TCP 443 refuses;
HTTP 80 redirects to `piazzaautogroup.com/locations/volkswagen.htm`
which returns 403 (Akamai BOT-BROWSER-IMPERSONATOR). Likely the dealer
has migrated or shut down. **Action**: verify whether the dealership is
still operating; if so, find their new schedule URL (possibly on the
parent `piazzaautogroup.com` group site).

**VW0005 Nanuet (ConnectCDK)** — The vehicle picker works in headless
(Make/Year/Model/mileage commit via React-aware native value setter,
URL advances to `/select-services`), but the services page never mounts
its tiles. Tried in this order, none succeeded:

1. `playwright-stealth` fingerprint patches (webdriver, plugins,
   chrome.runtime, sec-ch-ua, WebGL, etc.).
2. Real Chrome via `channel="chrome"` (not bundled Chromium).
3. `--disable-blink-features=AutomationControlled` launch flag.
4. Firefox via `pw.firefox.launch`.
5. WebKit via `pw.webkit.launch`.
6. `launch_persistent_context` with a temporary user-data dir.

ConnectCDK's `/select-services` bundle does a profile check we can't
slip past in headless. **Action**: run `uv run python
scripts/run_daily.py --headed` from an interactive macOS session — the
existing scraper should complete the flow with a visible browser. If
yes, we add a small `--headed-only-platforms=connect_cdk` knob to the
orchestrator so the daily cron can run mixed-mode (headless for Xtime,
headed for CDK).

## Action items, ranked

1. **(medium)** Try VW0005 Nanuet in headed mode — `uv run python
   scripts/scrape_one.py VW0005 --headed` from an active desktop
   session. If the services page renders, file a small change to mark
   CDK as headed-only and the launchd job will pick it up. Otherwise
   mark `active=false` like VW0001.
2. **(medium)** Verify VW0003 Piazza dealer status — phone the
   dealership at `(610) 896-4853`. If the dealership is operating from
   a new web address, update the registry.
3. **(low, do once 7+ days of data exist)** Build the analytics
   notebook (Slice 10): network-average lead time, next-day appointment
   rate, scheduling flow seconds heatmap. Code skeleton already in
   `notebooks/` placeholder.
4. **(deferred until scale matters)** Google Drive credentials and the
   GitHub Actions cron — see `SETUP.md` §A. Not blocking anything; just
   means the data lives only on this machine for now.

### Done

- ✅ Wired `append_to_timeseries` into `run_daily.py` — every run also
  updates `data/processed/timeseries.parquet`.
- ✅ Scheduled via launchd at 9 AM daily (plist in `~/Library/LaunchAgents/`).
- ✅ macOS notification on degraded or failed runs — silent on full
  success, banner with sound when `success_count < dealers_attempted`.
  Suppress with `--no-notify` for manual debugging.
- ✅ ConnectCDK walker extended for the free-text "Appointment Reason"
  service variant — reaches 11 of ~13 steps for VW0005 Nanuet in
  headed mode. Time-page date selectors still need mapping.

## What's in good shape

- 100 unit tests pass, mypy `--strict` clean.
- Three-envelope Xtime parser covers consumer.xtime, TeamVelocity, and
  legacy variants — adding new dealers on those platforms is a CSV row
  append.
- ConnectCDK parser is solid (handles bare list, dict-with-slot-key,
  and ISO-string slot lists).
- Real-world bug fixed: `RobotsCache` was being 403'd by DealerOn's
  Varnish on its default `Python-urllib` UA, silently disallowing
  Jeff. Now fetches `robots.txt` with our identifying UA.
- Walker handles three Xtime UI variants (consumer.xtime SPA, Jeff's
  iframe-embedded variant, TeamVelocity inline) and dismisses West
  Islip's "Already a customer?" intercepting modal.
- Output is idempotent: same-day re-runs replace the partition,
  verified with `wc -l` on the JSONL across two runs.
