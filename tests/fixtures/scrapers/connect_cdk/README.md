# ConnectCDK parser fixtures

Fixtures for `tests/scrapers/test_connect_cdk.py`. VW0005 (vwnanuet.com) loads
the ConnectCDK / VW SHIFT (COSA) scheduler in an iframe served from
`api.connectcdk.com`; its React app talks to
`nc-cdk-service-cosa-microservice.na.connectcdk.com` for dealer config and
availability data.

## Provenance

| File | Source |
|---|---|
| `slots_available/schedule_page.html` | **Real**: captured by `scripts/capture_connect_cdk_fixtures.py` from VW0005. Outer Dealer.com wrapper. |
| `slots_available/iframe_page.html` | **Real**: rendered CDK iframe (landing screen with "RETURNING CUSTOMER / NEW CUSTOMER"). |
| `slots_available/xhr_responses.jsonl` | **Real**: every XHR/fetch response from `connectcdk` hosts during the capture run. Pins the CDK microservice URL structure. |
| `slots_available/xhr_response.json` | **Synthesized**: mirrors the likely CDK availability envelope (top-level dict with `availableSlots` list). Slot field names (`startDateTime`, `displayTime`, `appointmentLength`) are best-guesses from common CDK COSA patterns, pending live validation in the follow-up live-wiring slice. |
| `slots_available/metadata.json` | **Real**: capture timestamp + URL + interaction trace. |
| `no_slots_available/xhr_response.json` | **Synthesized**: same envelope, empty `availableSlots` list. |
| `malformed_payload/xhr_response.json` | **Synthesized**: dict payload missing any recognized slot-list key. |
| `login_wall/iframe_page.html` | **Synthesized**: minimal HTML with CDK-style OTP/sign-in markers. |

## Why the slot field names are synthesized

The CDK landing page ("RETURNING CUSTOMER / NEW CUSTOMER") gates the rest of
the flow behind a customer-identification step. The capture script's
best-effort walk doesn't progress past that screen — reaching the
availability endpoint requires simulating a multi-step customer flow
(vehicle → service → calendar) we don't yet wire. Per SPEC.md's "no PII in
form fields" constraint and CLAUDE.md's "ask before large refactors", we
stop at the real entry capture and synthesize the downstream payload.

The parser (`src/vw_scraper/scrapers/connect_cdk.py`) is written to accept
either form CDK commonly returns — a bare list of slot dicts or a wrapper
dict keyed by `availableSlots` / `slots` / `appointments` / `items`. Live
validation against a real slot endpoint happens in the follow-up slice.

## Re-running the capture

```
uv run python scripts/capture_connect_cdk_fixtures.py            # headless
uv run python scripts/capture_connect_cdk_fixtures.py --headed   # see browser
uv run python scripts/capture_connect_cdk_fixtures.py --manual   # human walks flow
```

`--manual` is the path forward for live wiring: open the browser, click
through NEW CUSTOMER → vehicle → oil change → pick a date, and let the
script capture the slot XHR when the calendar renders.
