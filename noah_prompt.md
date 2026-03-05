Totally workable—you just want a path that doesn’t require you to become a developer.

Here are the easiest ways to do A (website-first) when you’re not very tech savvy, in order of “least technical” to “most control.”

1) Easiest: Hire a freelancer to build it (you provide the list + VIN)

What you do

Provide: your VW dealer list (CSV), the VIN, and the exact question: “earliest oil change appointment.”

Ask them to deliver:

a Google Sheet that auto-fills results,

a “Run” button (or scheduled run),

screenshots for each dealer as proof,

a short handoff doc + short Loom walkthrough.

Where to hire

Upwork / Toptal / Fiverr Pro / a local automation consultant

What to ask for (copy/paste)

“Build a Playwright-based website automation bot that visits each VW dealer’s online service scheduler, inputs this VIN, selects Oil Change, and returns the earliest available appointment date/time. Save results + a screenshot per dealer into Google Sheets. Must support at least Xtime + Tekion + DealerFX initially and flag captcha/login blockers.”

Cost reality

You’re usually paying for “vendor adapters.” First adapter costs the most; each additional one gets cheaper.

2) Low-tech: Use a Virtual Assistant team with a standardized checklist

If you just need this once (or monthly) and don’t need automation:

What you do

Put your dealers in a Sheet

Give a VA team the VIN + steps + what to record

Have them fill earliest availability + link to scheduler page

Pros

No build

Works even with weird schedulers/captchas

Cons

Labor cost scales linearly

Human variability unless you train tightly

This is honestly the fastest “non-tech” way to get a national comparison.

3) Medium-tech, very friendly: Use an RPA tool with a consultant (UiPath / Power Automate Desktop)

You don’t personally build it; a consultant sets it up and you just run it.

How it feels

You click “Run”

It opens a browser and “drives” the scheduling steps automatically

Exports results to Excel/Sheets

Downside

Still brittle across many scheduler types

Best for a smaller set of dealers

4) Best long-term but still non-tech for you: Get it built once, then you just “press a button”

This is basically Option 1 (Playwright) but you are only the operator.

Your experience

You open a simple dashboard (or Google Sheet)

Click “Run scan”

Come back later and see earliest appointments populated + links/screenshots

What you need to request

“One-click runner”

“Results go to Google Sheet”

“Auto-retry + clear failure categories”

“Screenshots for evidence”

If your goal is “compare across ALL VW dealers,” here’s the honest recommendation

If it’s nationwide, schedulers vary too much for a no-code tool to handle reliably.

Best non-tech approach:

VA team to get you a first dataset quickly (proof of value)

In parallel, hire someone to automate the top 2–3 scheduler vendors that cover most dealers

Anything blocked goes back to VA (or later Option B phone calls)

That hybrid keeps you moving without getting stuck in tooling.

Want me to set you up with a “done-for-you” spec you can send to a freelancer or VA?

Reply with:

roughly how many VW dealers you’re checking (all US? a region?),

how often you want to rerun it (one-time vs weekly),

and whether you care about “earliest time” or “earliest day only.”

Then I’ll give you:

a clean scope doc,

the exact spreadsheet columns,

the step-by-step VA checklist (if you choose VA),

and acceptance criteria so you actually get a usable bot.