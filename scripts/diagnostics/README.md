# Diagnostic scripts

Tools for debugging walker breakage or adding a new dealer. Not run by
the daily pipeline — invoke manually when something needs investigation.

## `probe_dealer_page.py`

Generic headless Playwright probe. Edit the `PROBES` list at the top of
the file to point it at any dealer's schedule URL. For each entry it
logs: final URL after redirects, page title, iframe srcs, count of
buttons / selects / inputs / "Schedule" links, scripts containing
platform-hint keywords, console errors, and the first 800 chars of body
text. Useful when adding a new dealer to confirm which scheduling
platform (Xtime, ConnectCDK, etc.) the dealer is on, and when a working
scraper suddenly breaks to see what changed.

```bash
uv run python scripts/diagnostics/probe_dealer_page.py
```

## Pattern for deeper walker debugging

When the walker is stuck at a step you can't see in headless, copy the
walker's `_try_fill_and_pick` / `_try_click` flow into a probe, get to
the stuck step, then `await tf.evaluate("...")` to dump DOM. The
session that fixed VW0005 Nanuet used this pattern repeatedly — see
the commit `744c9b9` for examples of what a final dump-after-step probe
looks like (it walked Vehicle → Service → catalog Oil-and-Filter →
disclaimer modal → page-NEXT → transport modal → transport-dialog-next
and dumped the time page).

## Discovery fixture (not yet ported here)

`scripts/discover_platforms.py` already exists at the repo root and
writes proper discovery fixtures to `tests/fixtures/discovery/`. Use
that for onboarding new dealers — it's the supported path. Edit
`data/dealer_master.csv` first to add the new dealer code + URL, then
run discovery to fill in `platform` and `config_json`.
