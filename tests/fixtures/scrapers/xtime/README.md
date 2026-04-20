# Xtime parser fixtures

Fixtures for `tests/scrapers/test_xtime.py`. The Xtime widget loads time slots
via XHR after vehicle and service selection; the rendered page DOM does not
contain slot data, so the parser operates on the JSON XHR payload.

## Provenance

| File | Source |
|---|---|
| `slots_available/schedule_page.html` | **Real**: captured by `scripts/capture_xtime_fixture.py` from VW0001 (Teddy VW). Entry/setup page only — no slots rendered because a registration modal gates progress. |
| `slots_available/xhr_responses.jsonl` | **Real**: every XHR/fetch response from `xtime.*` and `teamvelocity.*` hosts during the capture run. Includes the `/Xtime/Vehicle/Years` response which fixes the API envelope shape. |
| `slots_available/xhr_response.json` | **Synthesized**: mirrors the confirmed Xtime envelope shape (`{success, code, message, items, errorMsgForEndUser}`) from the real Years response, populated with realistic oil-change slot data. The slot field name (`startDateTime`) and item shape are best-guesses pending Slice 4 live validation. |
| `slots_available/metadata.json` | **Real**: capture timestamp + URL + interaction trace. |
| `no_slots_available/xhr_response.json` | **Synthesized**: same envelope, empty `items` array. |
| `malformed_html/schedule_page.html` | **Synthesized**: minimal broken HTML. |
| `login_wall/schedule_page.html` | **Synthesized**: minimal HTML with a sign-in marker. |

## Why the slot field names are synthesized

The Xtime API endpoint that returns slots requires a full registration
context (vehicle selected → service selected → date selected) which the
capture script could not reach without submitting dummy PII through a
registration modal. SPEC.md's "no PII in form fields" constraint and
CLAUDE.md's "ask before large refactors" steered us away from that path
in this slice.

The real envelope shape (`success`/`code`/`message`/`items`/`errorMsgForEndUser`)
is locked in by the captured Years response, so the parser's outer-shell
parsing is grounded in real data. The slot item field names will be
validated against a real slot response in **Slice 4**, where we'll wire
the scraper to a live browser session and refine if needed.

## Re-running the capture

```
uv run python scripts/capture_xtime_fixture.py            # headless, auto-walk
uv run python scripts/capture_xtime_fixture.py --headed   # see the browser
uv run python scripts/capture_xtime_fixture.py --manual   # human walks the form
```

`--manual` is the path forward in Slice 4: open the browser, complete the
flow with a real Volkswagen on hand (or dummy data, with explicit user
consent), and let the script capture the slot XHR.
