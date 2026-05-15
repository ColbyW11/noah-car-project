# Production Deployment Plan

How to take this pipeline from "Colby's laptop with launchd" to a
production environment that doesn't depend on a single laptop staying
on and awake. Aimed at a small startup (1–3 people, no DevOps hire),
not enterprise scale.

For current local-mode state and operational details, see
[`STATUS.md`](./STATUS.md). This file is forward-looking only.

---

## TL;DR — pick one of four paths

| Path | Monthly cost | Setup time | Best for |
| --- | --- | --- | --- |
| **0. GitHub Actions (you already use it)** ⭐ | $0 | 30 min | Solo founder, ≤30 dealers, daily cadence, no public API yet |
| **A. Single VM ("boring stack")** | $5–15 | 2–4 hrs | When you outgrow GH Actions: need an API surface, anti-bot blocks GH runners, or >100 dealers |
| **B. Managed PaaS (Fly.io / Railway)** | $15–40 | 1–2 hrs | Want zero infra ops, fine paying a small premium |
| **C. GCP serverless (Cloud Run Jobs + GCS + BigQuery)** | $20–80 | 4–8 hrs | Plan to scale to 500+ dealers, want analytics-grade storage from day one |

**Recommendation**: start with **Path 0** (GitHub Actions). You're
already on GitHub, the workflow file is already mostly written, the
free tier covers daily runs forever, and there's zero infrastructure
to manage. Migrate to Path A only when something specific forces you
off — most likely a dealer blocking Azure datacenter IPs, or wanting
a public API on the same box. Skip B unless you specifically dislike
SSH. Path C is the eventual destination only if the startup grows
into hundreds of dealers.

---

## What this plan changes vs. the current setup

| Concern | Today (local) | Production target |
| --- | --- | --- |
| Compute | Colby's MacBook | A Linux box (VM or container) that's always on |
| Scheduling | macOS launchd | Linux cron, app-level scheduler, or managed (Cloud Scheduler / GitHub Actions) |
| Storage | `data/raw/`, `data/processed/` on disk | Object storage (S3/R2/GCS) + a query layer (DuckDB or BigQuery) |
| Alerts | macOS notification banner | Email or Slack via Healthchecks.io / Sentry |
| Secrets | `.env` files, `~/.config/vw-scraper/` | Cloud secrets manager or `.env` via host secrets |
| Browser | Real Chrome via Playwright | Headless Chromium in a Playwright Docker image |
| Public API | None | Optional — FastAPI on the same box, only when needed |

---

## Path 0 — GitHub Actions [RECOMMENDED]

You already use GitHub for the repo. Everything you need — scheduling,
secrets, logs, monitoring, free compute — is already there. The
existing `.github/workflows/daily-scrape.yml` is 90% wired; it's
commented out only because it was originally gated on Drive credentials
that you haven't set up.

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│ GitHub Actions (free tier: 2000 min/mo, unlimited public)│
│                                                          │
│  on: schedule: "0 13 * * *"   # daily 9 AM ET            │
│  ┌────────────────────────────────────────────────────┐  │
│  │ ubuntu-latest runner (fresh each run)              │  │
│  │   uv sync                                           │  │
│  │   playwright install chromium                       │  │
│  │   python scripts/run_daily.py                       │  │
│  │   git push data/ to <data-repo>                     │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
        │ on failure / degraded
        ▼
  Slack webhook (or just email-on-failure, which GH does for free)
```

### Concrete steps (~30 minutes)

1. **Decide where data lives.** Pick one — easiest first:
   - **A separate `vw-scraper-data` GitHub repo.** Workflow `git push`es
     `data/raw/` and `data/processed/` after each run. Zero new
     accounts. After a year of daily runs you'll have ~365 small JSONL
     files (a few KB each) and one growing parquet — well under any
     GitHub size limit.
   - **Cloudflare R2.** Free egress, $0.015/GB/mo. One signup, one
     access-key secret in GH. Cleaner separation but adds an account.
   - **Drive sync as already wired in `scripts/ci_run.py`.** Free, uses
     a GCP service account. The workflow file is already written for
     this; you just complete the SETUP.md §A checklist.
2. **Re-enable the schedule.** In `.github/workflows/daily-scrape.yml`,
   uncomment the `schedule:` block and (if you went with option 1
   above) replace the Drive-sync step with a `git push` step.
3. **Add the secrets** via `gh secret set`. At minimum
   `VW_SCRAPER_SLACK_WEBHOOK` for alerts; plus whichever storage
   secrets your data-destination needs.
4. **Test with `workflow_dispatch`.** The workflow already supports
   manual runs — push a button in the Actions tab, watch it run, fix
   anything that breaks.
5. **Shut down the laptop launchd jobs** after a week of clean GH runs:
   `launchctl bootout gui/$UID/com.colby.vw-scraper.daily` (and the
   weekly/logrotate ones).

### What's already done for you

- `.github/workflows/daily-scrape.yml` exists and runs `scripts/ci_run.py`
- `scripts/ci_run.py` already handles degraded-run detection (>25%
  dealer failure) and self-alerts via Slack
- `src/vw_scraper/alerts.py` already has `send_slack_alert()` wired
- `--with-deps` flag on `playwright install` is already there, so
  Ubuntu's missing libraries get installed automatically

### Costs (Path 0)

- GitHub Actions: $0 on the free plan
  - Public repo: unlimited minutes
  - Private repo: 2000 min/mo free; one daily ~10-min run = 300 min/mo
- Data storage (whichever you chose): $0–1/mo
- Slack: $0 (free workspace, incoming webhook)
- Optional Sentry: $0 (5K events/mo free)

**Total: $0/mo** for the first year, easily.

### Tradeoffs

✅ Literally nothing to provision or patch
✅ Logs, secrets, scheduling, retries all in one UI you already use
✅ Each run is a fresh runner — no "the VM is in a weird state" debugging
✅ Free, indefinitely
✅ Reverting a broken change is `git revert + push`
❌ **Anti-bot risk**: Azure datacenter IPs vary per run. If a dealer
   eventually 403s the runners, you can't easily add residential
   proxies (you'd hit Path A or buy a proxy service).
❌ **No always-on surface**: when you want a "fastest oil change near
   me" API or a Streamlit dashboard, GH Actions can't host that. You'll
   add a VM (or Vercel/Cloud Run) when that day comes.
❌ **Edit-yaml-push-wait debug loop** is slower than SSH-and-iterate.
   Not a problem for steady-state, painful during dealer-walker debugging.
❌ **2000-min/mo cap** on free private repos. Each daily run is ~10
   min, so you have headroom for ~6× the current schedule before
   hitting it. Public repo = no cap.

### When to migrate off Path 0

- A dealer blocks the runner IPs (you see consistent 403s in workflow logs)
- You add a consumer-facing API or dashboard that needs to be always-on
- You're running more than ~100 dealers and the workflow takes >30 min
- You want to do interactive `--headed` debugging more than once a month

---

## Path A — Single VM ("boring stack")

The whole pipeline runs on one Linux box. No managed services except
object storage (cheap) and an uptime monitor (free).

### Architecture

```
┌────────────────────────────────────────────────────────┐
│ Hetzner CX22 / DO 2GB droplet ($5–12/mo)               │
│                                                        │
│  ┌──────────────┐    ┌───────────────────────────┐    │
│  │ cron 9 AM    │───>│ docker run vw-scraper:    │    │
│  │ cron 4 AM    │    │   - scrape all dealers    │    │
│  │ cron Sun 10  │    │   - write JSONL + parquet │    │
│  └──────────────┘    │   - rclone to R2          │    │
│                      └───────────────────────────┘    │
│                                                        │
│  /var/lib/vw-scraper/data/                            │
│    raw/2026-05-14/observations.jsonl                  │
│    processed/timeseries.parquet                       │
└────────────────────────────────────────────────────────┘
        │ rclone sync (hourly)
        ▼
┌────────────────────────────────────────┐
│ Cloudflare R2 bucket (free egress,     │
│ $0.015/GB/mo, ~$0.02/mo at 1GB)        │
│   vw-scraper-data/                     │
│     raw/<date>/...                     │
│     processed/timeseries.parquet       │
└────────────────────────────────────────┘
        │ on demand (DuckDB query)
        ▼
   Analytics — DuckDB reads parquet directly from R2
```

### Concrete steps

1. **Provision the VM.** Hetzner CX22 (Ashburn or Falkenstein, $5/mo,
   2 vCPU / 4GB) or DigitalOcean 2GB droplet ($12/mo). Ubuntu 24.04.
   - Hetzner is cheaper and faster CPU-for-CPU; DO has nicer dashboard.
2. **Harden it.** `ufw allow OpenSSH`, disable password auth, create
   non-root user, fail2ban.
3. **Install Docker.** `apt install docker.io`.
4. **Build the image.**
   ```dockerfile
   FROM mcr.microsoft.com/playwright/python:v1.50.0-noble
   WORKDIR /app
   COPY pyproject.toml uv.lock ./
   RUN pip install uv && uv sync --frozen --no-dev
   COPY src/ src/
   COPY scripts/ scripts/
   COPY data/dealer_master.csv data/dealer_master.csv
   ENTRYPOINT ["uv", "run", "python"]
   ```
   Playwright's image already has Chromium + system deps installed,
   which removes the single biggest deployment headache.
5. **Schedule with cron.**
   ```
   0 13 * * *   docker run --rm -v /var/lib/vw-scraper/data:/app/data ghcr.io/<you>/vw-scraper:latest scripts/run_daily.py
   0 8  * * *   docker run --rm -v /var/lib/vw-scraper/data:/app/data ghcr.io/<you>/vw-scraper:latest scripts/rotate_logs.py
   0 14 * * 0   docker run --rm -v /var/lib/vw-scraper/data:/app/data ghcr.io/<you>/vw-scraper:latest scripts/health_check.py
   ```
   (UTC. 13:00 UTC = 9 AM Eastern in summer.)
6. **Mirror to R2.** Install `rclone`, configure an R2 remote, add
   `15 13 * * * rclone sync /var/lib/vw-scraper/data r2:vw-scraper-data`.
   Why R2 over S3: zero egress fees, which matters once you're pulling
   parquet from your laptop for ad-hoc analysis.
7. **Wire uptime monitoring.** Sign up at healthchecks.io (free for 20
   checks). Append `&& curl -fsS https://hc-ping.com/<uuid>` to each
   cron line. Healthchecks will Slack/email you if a job doesn't ping
   within the expected window.
8. **Alerts.** Replace the `osascript` notifier with either:
   - A Slack webhook (one-line change in `scripts/run_daily.py`'s
     `_maybe_notify`), OR
   - Wire `sentry-sdk` (free tier: 5K events/mo) and capture exceptions
     in the orchestrator.

### Costs (Path A)

- VM: $5/mo (Hetzner) or $12/mo (DO)
- R2: ~$0.02/mo at current data volume; budget $1/mo within a year
- Healthchecks.io: $0
- Sentry: $0 (free tier)
- Domain (optional, if you add a public API): $12/yr

**Total: ~$5–15/mo** for the first year.

### Tradeoffs

✅ Cheapest, full control, easy to SSH and inspect when something breaks
✅ Playwright Just Works on the Microsoft image — no Lambda layer drama
✅ Logs are normal files; you can `tail -f`
❌ Single point of failure (VM goes down → no scrapes that day). Acceptable for daily-cadence data.
❌ You're responsible for OS patches. `unattended-upgrades` handles 90% of this.
❌ No autoscaling. Fine — you don't need it for ≤500 dealers on a daily cadence.

---

## Path B — Managed PaaS (Fly.io or Railway)

Same logical architecture as Path A, but the host abstracts away the VM.

### Variants

- **Fly.io**: deploy a Docker image as a "machine"; use `fly machine
  run` with a cron schedule (Fly added native cron via `[[mounts]]` +
  `[deploy]` schedule in 2024). Persistent volume for `data/`.
- **Railway**: deploy from a `Dockerfile`, add a Cron service from the
  UI. Volumes are managed.
- **Render**: Cron Jobs (their term) plus a Persistent Disk. Simplest UI of the three.

### Why I'd skip this

Fly is genuinely great for stateless web apps, but Playwright workloads
hit two friction points:

1. **Memory ceilings.** A `shared-cpu-1x@1024MB` machine ($1.94/mo) is
   too small for Chrome — it'll OOM mid-scrape. You'll end up on
   `performance-2x@2048MB` ($31/mo), at which point Hetzner CX22 is
   cheaper, has more CPU, and lets you SSH in.
2. **Debugging is harder.** When the walker fails in headless mode and
   you need to attach a debugger or run `--headed` against a tunneled
   X server, the SSH-and-iterate loop on a VM is much faster than
   "edit code → push → wait for redeploy → tail logs → repeat."

If you specifically want to avoid managing a Linux box, Render's UX is
the best of the three for this workload. Budget $20–40/mo all-in.

---

## Path C — GCP serverless (Cloud Run Jobs + GCS + BigQuery)

The "scales to 1000 dealers without thinking about it" path. Overkill for
now but the right destination if the startup takes off.

### Architecture

```
                ┌─────────────────────────────────┐
                │ Cloud Scheduler (cron)          │
                │   trigger daily 13:00 UTC       │
                └────────────────┬────────────────┘
                                 ▼
                ┌─────────────────────────────────┐
                │ Cloud Run Job: vw-scraper       │
                │   - playwright/python image     │
                │   - 2 vCPU / 2 GiB / 30 min cap │
                │   - reads dealer_master from    │
                │     GCS, writes JSONL + parquet │
                │     back to GCS                 │
                └────────────────┬────────────────┘
                                 ▼
        ┌────────────────────┐   ┌───────────────────────┐
        │ GCS bucket         │   │ BigQuery external     │
        │ vw-scraper-data/   │──>│ table over parquet    │
        │   raw/<date>/...   │   │ + materialized views  │
        │   processed/*.parq │   │ for daily metrics     │
        └────────────────────┘   └───────────────────────┘
                                          ▼
                            Looker Studio / Metabase dashboards
```

### Why "scales without thinking"

- Cloud Run Jobs run to completion then go to zero. You pay per-execution
  (free tier covers 2M vCPU-seconds and 180K GiB-seconds/mo — plenty for
  one daily 5-minute run, even with 100s of dealers).
- BigQuery's external-table-over-parquet pattern means you never load
  data into a warehouse — DuckDB-on-laptop and BigQuery hit the same
  GCS files.
- Cloud Scheduler is $0.10 per job per month; you'll have 3 jobs = $0.30.

### Why it's not the recommended starting point

- More moving parts (Cloud Run + Scheduler + GCS + IAM + BigQuery
  dataset). Each one is simple; debugging across all four when something
  fails is what eats time.
- Cloud Run has a **60-minute** maximum execution timeout (was 30 min
  until 2024). With ~120s per dealer at current concurrency, 30 dealers
  = ~10 min. Scales to a few hundred dealers, then you'd need to shard.
- You write IaC (Terraform / Pulumi) or you click through the console
  and lose reproducibility. Either way, more setup than `cron + docker
  run`.

### When to migrate from A → C

- Data exceeds ~50GB and the VM SSD is full
- You need >99% scheduling reliability (laptop-style "if the VM is down
  we miss a day" stops being acceptable)
- You hire someone who already knows GCP

### Costs (Path C, ~30 dealers daily)

- Cloud Run: $0–5/mo (mostly within free tier)
- GCS: ~$0.50/mo at 20GB
- Cloud Scheduler: $0.30/mo
- BigQuery: $0 (1TB free queries/mo)
- Secret Manager: ~$0.06/mo per secret

**Total: ~$5–10/mo for tiny workloads, climbs with scale.** The win
isn't cost — it's that you don't manage a VM.

---

## Concern-by-concern options

If you want to mix and match instead of picking a path wholesale.

### Compute (where the scraper runs)

| Option | Cost | Pros | Cons |
| --- | --- | --- | --- |
| **GitHub Actions cron** ⭐ | $0 (within 2K min/mo on private; unlimited on public) | Zero infra, secrets + logs + cron all in the GH UI you already use | Azure datacenter IPs vary per run — could trip anti-bot; no always-on surface for an API |
| Single VM (Hetzner/DO) | $5–12/mo | Cheap, easy debug, no cold-start, can host an API too | You patch the OS |
| Cloud Run Job | $0–10/mo | Auto-scaling, zero ops, free tier | 60-min cap; complex IAM |
| AWS Fargate scheduled task | $5–20/mo | Same scale benefits as Cloud Run | ECS/IAM are noisier than GCP |
| AWS Lambda + Chromium layer | $0–5/mo | Cheapest at low volume | 15-min cap, Chromium-not-Chrome breaks Xtime |
| Browserless / Browserbase | $50–200/mo | Managed Chrome with stealth presets | Expensive once you have ≥3 dealers; vendor lock-in |

**Recommendation**: GitHub Actions until you outgrow it. Then a single
VM (skipping all the serverless options) because the "scraper that
drives a real browser plus eventually serves an API" workload is
exactly what serverless is bad at.

### Storage

| Option | Cost | Pros | Cons |
| --- | --- | --- | --- |
| **Cloudflare R2** ⭐ | $0.015/GB/mo, $0 egress | No egress fees → cheap to query from laptop | Smaller ecosystem than S3 |
| AWS S3 | $0.023/GB/mo + egress | Universal tool support | Egress fees bite once you pull data regularly |
| Google Cloud Storage | $0.020/GB/mo + egress | Free tier, BigQuery integration | Egress fees |
| Backblaze B2 | $0.006/GB/mo | Cheapest at rest | Slower; egress fees outside Cloudflare |
| Postgres (Neon / Supabase) | $0–25/mo | Queryable from the start | Mismatch — slot data is time-series, not relational |

**Recommendation**: R2 for raw + parquet. Add Postgres later only if
you build a public API that needs OLTP queries.

### Analytics / query layer

| Option | When to use |
| --- | --- |
| **DuckDB against parquet** ⭐ | Always start here. Reads parquet from R2 directly; <10GB scales fine on a laptop. |
| BigQuery external tables | When DuckDB-on-laptop becomes slow (~50GB+) and you want concurrent queries |
| ClickHouse Cloud | If you go heavy time-series and need sub-second analytical queries |
| Snowflake | Don't. Wrong tier of product for a small startup. |

### Scheduling

| Option | Best for |
| --- | --- |
| **Linux cron on the VM** ⭐ | Path A. Reliable, well-understood, observable via Healthchecks.io. |
| Cloud Scheduler (GCP) | Path C. Cheapest managed cron. |
| GitHub Actions schedule | Code lives in GH, single-stack simplicity, willing to absorb minute usage |
| Temporal / Inngest | When you grow into multi-step workflows with retries and observability needs |

### Observability

| Option | Cost | What it gives you |
| --- | --- | --- |
| **Healthchecks.io** ⭐ | Free (20 checks) | "Did the cron run?" alerting. Highest-ROI single signup. |
| **Sentry** ⭐ | Free (5K events/mo) | Exception tracking with stack traces. Wire `sentry-sdk` in 10 LOC. |
| Better Stack (Logtail + Uptime) | $0–25/mo | Centralized logs + uptime in one dashboard |
| Grafana Cloud free tier | $0 | Metrics + logs if you want to graph slot counts over time |
| Datadog | $15/host/mo | Don't. Overkill at this scale, and the bill grows fast. |

**Minimum viable observability**: Healthchecks.io + Sentry + a Slack
webhook for the orchestrator's degraded-run notifier. Three free
signups, ~30 min total wiring.

### Anti-bot / IP rotation

You haven't hit blocking yet (3 of 3 dealers working from a residential
IP), but datacenter IPs are more likely to get challenged. Options if
that happens:

| Option | Cost | When |
| --- | --- | --- |
| Status quo (datacenter IP from VM) | $0 | Start here. If it works, stop. |
| Residential proxy via Bright Data / Oxylabs | $5–15/GB | When ≥1 dealer 403s reproducibly from the VM IP |
| Run the VM on a residential ISP (Tailscale Funnel → home laptop) | $0 | Cute but fragile — you're back to laptop-dependency |
| Browserbase / Browserless residential | $50+/mo | Last resort; offload the whole problem |

**Heuristic**: don't pay for proxies until a dealer measurably blocks
you. Datacenter IPs are fine for low-volume daily scraping.

### Secrets

| Option | Best for |
| --- | --- |
| **`.env` file on the VM, `chmod 600`** ⭐ | Path A. Boring and effective. Back up to a password manager. |
| GitHub Actions secrets | Path B if you deploy via GH |
| GCP Secret Manager | Path C; ~$0.06/secret/mo |
| Doppler / Infisical | Multi-environment / team setups (not yet) |

### Public API surface (only when you need it)

Once the scraper has accumulated data and you want a "fastest oil
change near me" consumer surface:

| Option | Best for |
| --- | --- |
| **FastAPI on the same VM, served by Caddy** ⭐ | Path A. Same box, same code. Caddy handles HTTPS automatically. |
| Cloud Run service over BigQuery | Path C. Scales to consumer traffic without thinking. |
| Vercel + serverless functions | If the frontend lives there too and queries are short |

Don't build the API until you have a consumer surface that actually
needs it. The scraper + parquet is a complete product for internal
analytics.

---

## Migration from current laptop setup

Concrete day-by-day if you commit to Path 0 (recommended):

**Day 1 — pick data destination, write the push step**
- [ ] Decide: separate data repo, R2, or Drive (in order of simplicity)
- [ ] If data repo: `gh repo create vw-scraper-data --private`
- [ ] If R2: sign up, create bucket, generate API token, save the 4
      env-var values you'll need (account ID, access key, secret key,
      bucket name)
- [ ] If Drive: complete SETUP.md §A (you already have a Google account)

**Day 2 — wire and test**
- [ ] In `daily-scrape.yml`: uncomment `schedule:`, replace the Drive
      step with whichever push step matches your choice
- [ ] `gh secret set VW_SCRAPER_SLACK_WEBHOOK` (and any storage secrets)
- [ ] In the GH Actions UI, dispatch the workflow manually
- [ ] Check that data lands where you expect; Slack notification fires on
      forced failure (temporarily break a selector to test)

**Day 3 through day 10 — parallel run**
- [ ] Leave both the laptop launchd jobs and the GH cron running
- [ ] Compare outputs after 7 days. Same dealers succeed in both?
- [ ] If yes → `launchctl bootout` all three local plists
- [ ] If no → diagnose the divergence before shutting down the laptop

**After cutover — let it bake**
- [ ] Watch the Actions tab once a week for a few minutes
- [ ] Add Sentry when you have a free 10 min — `uv add sentry-sdk` and
      `sentry_sdk.init(dsn=os.environ["SENTRY_DSN"])` in `ci_run.py`

---

## (Alternative) Migration from current laptop setup to Path A

Concrete week-by-week if you commit to Path A instead:

**Week 1 — provision and dockerize**
- [ ] Spin up a Hetzner CX22 or DO 2GB droplet. SSH hardening.
- [ ] Write a `Dockerfile` based on `mcr.microsoft.com/playwright/python`.
- [ ] Build locally, smoke-test `docker run ... scripts/scrape_one.py VW0002`.
- [ ] Push to GHCR (free for public images; private images need a GH token).

**Week 2 — wire scheduling and storage**
- [ ] Install Docker on the VM, pull the image.
- [ ] Set up cron jobs for daily / weekly / logrotate (UTC times).
- [ ] Configure rclone → R2 sync after each daily run.
- [ ] Verify a manual `cron run -v` produces the same output you see locally.

**Week 3 — observability and shut down the laptop**
- [ ] Wire Healthchecks.io ping into each cron line.
- [ ] Add `sentry-sdk` to the orchestrator.
- [ ] Replace `osascript` notifier with a Slack webhook (10-line change).
- [ ] Leave the laptop launchd jobs running for one week in parallel as a fallback.
- [ ] After 7 successful days on the VM, `launchctl bootout` the laptop plists.

**Week 4 — let it bake**
- [ ] Don't add anything. Watch it run.
- [ ] If a dealer breaks, fix it; otherwise leave it alone.
- [ ] After a month of stable runs, decide whether to add the next dealer
      cohort or build the API surface first.

---

## What to defer until you have customers

These are tempting but premature for a pre-revenue scraper:

- **Kubernetes / ECS / any orchestrator.** A single Docker container on
  cron is enough until you're running ≥5 jobs in parallel.
- **A proper data warehouse.** DuckDB-over-parquet handles 100GB without
  blinking. Snowflake/BigQuery only matter when multiple humans query
  concurrently.
- **Multi-region.** Daily scraping doesn't care about latency. One US-East
  VM is fine.
- **A staging environment.** Run new dealer walkers against the prod VM
  in `--headed` mode over SSH X-forwarding when you need to debug.
  Building a second VM "for staging" doubles cost without doubling value.
- **CI/CD beyond GitHub Actions push-to-GHCR.** Manual `docker pull &&
  systemctl restart` on the VM is fine for a solo founder.
- **A real frontend.** If the consumer surface is "fastest oil change
  near me," start with a Streamlit / Next.js page over the DuckDB layer
  before committing to a full app.

---

## Decision checkpoints

Use these as triggers to revisit the plan, not blockers up front:

| Trigger | Reconsider |
| --- | --- |
| Adding 5th+ dealer behind a different platform | Whether the walker pattern generalizes or you need a per-vendor strategy |
| Parquet exceeds 5GB | Whether to partition by month and start using BigQuery / DuckDB partitioned tables |
| Two consecutive days of cron failure | VM upgrade, or move to Cloud Run for managed retries |
| First paying customer | Build the API surface; add Postgres for user accounts |
| First 1000 dealers in the registry | Migrate to Path C (Cloud Run) — single VM stops being enough |
| First dealer 403s the VM IP | Add a residential proxy for that dealer only |

---

## Open questions to answer before week 1

1. **Where does the company's code live now — GitHub, GitLab, Bitbucket?**
   Path A's GHCR step assumes GitHub. Swap for GitLab Container Registry
   or AWS ECR if not.
2. **Is the eventual consumer product B2C or B2B?** Affects whether the
   data API needs auth (B2B = API keys) or rate limiting (B2C = abuse
   prevention).
3. **Do you want logs in a UI or are SSH + grep fine?** Drives the
   Better Stack vs. status-quo decision.
4. **Any legal/contractual obligation about where the data is stored?**
   (E.g., a dealer agreement requiring US-only storage.) Affects R2 vs.
   S3 region selection.

If the answers are "GitHub / undecided / SSH is fine / no constraints,"
go with Path A as written.
