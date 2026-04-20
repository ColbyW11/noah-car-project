# Claude Code Kickoff Guide

## Before you open Claude Code

1. Create an empty directory and drop these files into it:
   - `SPEC.md`
   - `CLAUDE.md`
   - `SLICES.md`
   - `README.md`
   - `.env.example`
   - `.gitignore`
   - `data/dealer_master.csv`
   - `KICKOFF.md` (this file)

2. `git init` and make an initial commit containing only these docs. This gives you a clean baseline to diff against.

3. Have your 5 dealer URLs ready — you'll need them at Slice 2.

4. If you want Drive sync working from day one (Slice 7): create a Google Cloud service account, grant it access to your Drive folder, and download the JSON key to `~/.config/vw-scraper/service_account.json`.

## Session 1 prompt (copy this verbatim into Claude Code)

```
Read SPEC.md, CLAUDE.md, and SLICES.md in full before doing anything else.

Then execute Slice 0 from SLICES.md. Do not proceed past Slice 0 in this session.

At the end:
1. Run the full test suite and confirm it passes.
2. Show me the file tree you created.
3. Summarize what you did in 3–5 bullets.
4. Wait for my confirmation before proposing the next slice.
```

## Sessions 2 through N

Start each subsequent session with:

```
Read SPEC.md and CLAUDE.md. Confirm the current state: run the test suite and tell me which slice from SLICES.md is next.

Then execute Slice [N]. Follow the session hygiene rules at the end of SLICES.md.
```

Only do one slice per session. It's the single most important discipline for keeping output quality high.

## When to break the rules

- **Small fixes** (typos, log tweaks, single-file refactors) can skip the full slice ritual. Ask for them directly.
- **Exploratory questions** ("how does the Xtime page differ from the myKaarma one?") don't need a slice — they're research.
- **Slice 2 requires your input.** You'll paste the 5 dealer URLs when prompted.

## Red flags during a session

Stop the session and rethink if any of these happen:

- Claude Code is modifying files not named in the current slice.
- Tests are being deleted or made less strict to get them passing.
- The agent is stuck in a loop (3+ failed iterations on the same error).
- SPEC.md or CLAUDE.md are being edited without a clear reason.
- A slice is taking more than ~30 minutes of active work.

When any of these happen: interrupt, commit what's working, reread the slice, and start a fresh session with a tighter prompt.

## What good looks like at the end of Slice 5

You can run `uv run python scripts/run_daily.py` and get:

- A file at `data/raw/2026-04-19/observations.jsonl` with 5 lines.
- Each line is a valid `ScrapeResult` JSON.
- At least 3 of 5 dealers have `status='success'` with real slot data.
- Failed dealers have meaningful error messages, not stack traces.
- Total runtime under 2 minutes.

That's the milestone worth celebrating. Everything after is scaling and polish.
