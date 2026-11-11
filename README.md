# Internship Search Agent

A personal job-search agent that searches broadly across the internship market,
deduplicates the same job across sites, ranks what is left against your profile,
and sends a short digest to your phone.

> **Search broadly. Deduplicate aggressively. Rank intelligently. Notify selectively.**

Your preferred-company list is a **ranking preference, not a search boundary**.
The system discovers employers you have never named — see
[Company discovery](#company-discovery).

---

## Quick start

```bash
python -m venv .venv

# Activate it -- every `python` below must be the venv's interpreter,
# otherwise you get ModuleNotFoundError: No module named 'pydantic_settings'.
.venv\Scripts\Activate.ps1     # Windows (PowerShell)
# .venv/Scripts/activate.bat    # Windows (cmd.exe)
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"

cp .env.example .env          # works as-is; no credentials required
python -m alembic upgrade head
python -m app.cli seed        # populate the company + ATS registry
python -m app.cli search      # run the pipeline once
python -m app.cli serve       # dashboard at http://127.0.0.1:8000
```

Not activating is optional -- you can call the interpreter directly instead,
e.g. `.venv/Scripts/python -m app.cli serve`. What does *not* work is
installing into `.venv` and then running a bare `python`, which resolves to
your system interpreter.

The MVP runs with **zero credentials**. Everything in Tier 1–3 below is public.

---

## What actually happens on a run

```
                            JOB MARKET
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
   Curated lists           ATS platforms          Job boards / APIs
   (GitHub, HN)      (Greenhouse, Lever, Ashby,   (Muse, Remotive,
                      Workday, SmartRecruiters,    Adzuna, JSearch…)
                      Workable, Recruitee)
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                ▼
                        Raw listings
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
             Prefilter (cheap)      Board harvesting  ──┐
                    │                (company discovery) │
                    ▼                                    │
              Normalization                              │
                    │                                    │
                    ▼                                    │
          Deduplication (5 stages)                       │
                    │                                    │
                    ▼                                    │
              Canonical jobs                             │
                    │                                    │
                    ▼                                    │
             Relevance scoring                           │
                    │                                    │
          ┌─────────┴─────────┐                          │
          ▼                   ▼                          │
      Dashboard         Notification                     │
                                                         │
   ATS registry ◀────────────────────────────────────────┘
   (crawled directly on the next run)
```

Board harvesting runs on the **unfiltered** listing set, so an unknown employer
still enters the registry even when none of its current openings match you.

---

## Job sources

Every source implements one interface (`app/sources/base.py`), so adding
another is a subclass plus one line in the registry.

### Tier 1 — public ATS APIs (no credentials)

| Source | Notes |
|---|---|
| Greenhouse | Public job board API |
| Lever | Public postings API |
| Ashby | Public posting API |
| SmartRecruiters | Public postings API |
| Workable | Public account jobs API |
| Recruitee | Public offers API |
| **Workday** | The endpoint each tenant's own careers page calls. Highest yield for large hardware/semiconductor employers. Uses the tenant's own *intern* facet when it exposes one, which is both precise and reliably paginated. |

### Tier 2 — public boards and aggregators (no credentials)

The Muse · Remotive · Arbeitnow · Hacker News "Who is Hiring" (startup discovery)

### Tier 3 — curated lists (no credentials)

SimplifyJobs and community internship lists. These are the richest **seed** for
company discovery: one fetch yields thousands of postings whose URLs point
straight at employer ATS boards.

### Tier 4 — credentialed (all optional)

| Source | Env vars | Cost |
|---|---|---|
| Adzuna | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | free tier |
| JSearch (Google Jobs / LinkedIn / Indeed) | `JSEARCH_API_KEY` | free tier on RapidAPI |
| SerpApi Google Jobs | `SERPAPI_KEY` | paid |
| USAJOBS | `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | free |
| Jooble | `JOOBLE_API_KEY` | free on request |

A source with missing credentials reports itself **unconfigured** and the run
continues. It is never counted as searched.

### What this system deliberately does not do

**It does not scrape LinkedIn, Indeed, Glassdoor, ZipRecruiter, or Handshake.**
Their terms prohibit it, they actively block it, and anything built on it breaks
within weeks. Those postings still reach you through **licensed resellers**
(Adzuna, JSearch, SerpApi), which is the legitimate path. The coverage dashboard
labels them accordingly.

---

## Deduplication

The same internship on six sites must appear **once**; two different openings
must never merge. Five escalating stages feed a union-find cluster builder.

| Stage | Signal | Certainty |
|---|---|---|
| 0 | `(source, source_job_id)` | exact |
| 1 | Canonical URL (tracking/affiliate/session params stripped, redirect wrappers unwrapped) | exact |
| 2 | **ATS identity** — `provider:board:req_id` parsed from *any* URL, including aggregator apply-links | exact |
| 3 | Fingerprint — `company + title_core + location + employment_type` | inferential |
| 4 | Similarity — title tokens, description shingles, requisition ids, posting dates | inferential |
| 5 | LLM adjudication, uncertain band only, merges only on high confidence | inferential |

Stage 2 is the mechanism that makes cross-board dedup work: if an aggregator's
apply-link resolves to `greenhouse.io/acme/jobs/123` and the Greenhouse crawler
also fetched that posting, they are *provably* the same job.

### Anti-over-merge guards

Stages 3–5 must clear `merge_guard()`. Two listings never merge when they have:

- different companies
- **distinct requisition IDs**
- conflicting ATS identities
- conflicting discriminators — `verification` vs `design`, `summer` vs `fall`,
  `intern` vs `co-op`, `hardware` vs `software`, `PhD` vs `Masters`
- incompatible **metros** (Austin ≠ Santa Clara)
- different employment types
- **the same source with different URLs** — a board does not advertise one job
  twice under two ids, so these are distinct openings

### Canonical application URL

When duplicates merge, the Apply button resolves to the most authoritative link:

```
company careers page  →  ATS page  →  major job board  →  aggregator
```

---

## Ranking

Score is a weighted blend of seven components, each reporting its own reasons,
so the dashboard can always explain the number:

| Component | Default weight |
|---|---|
| Role match | 25% |
| Technical skills | 25% |
| Candidate fit | 15% |
| Location | 10% |
| Freshness | 10% |
| Internship constraints | 10% |
| Company preference | 5% |

### Relevance gates; context modulates

The seven components are not simply added. Two of them — role match and
technical skills — decide whether this is the right *kind* of job. The other
five describe how good a *relevant* job is, so they scale the result rather
than adding to it:

```
score = relevance × (0.55 + 0.45 × context/100)
```

Adding them instead gave every posting a floor of roughly 32 points from
components that barely vary across a corpus (an internship is an internship; a
location is usually fine). A job matching neither the role nor a single skill
still scored ~35, which squeezed 5,000 real postings into the 30–60 band and
made an absolute threshold meaningless. Worse, it mis-ranked: a generic "AI
Software Engineer Intern" outscored an "RTL Intern" for a hardware profile,
because being recent and nearby outweighed being the wrong job.

Under the gate the same corpus spreads across 0–95, and the ordering matches
the profile: FPGA and computer-architecture roles at the top, generic software
and quant roles well below. Absence of evidence stays neutral rather than
negative — a posting with no extractable skills is not punished for it.

Weights are normalised, so custom values need not sum to 100. Priority bands
(`🔥 Apply now` ≥ 90, `⭐ Strong match` ≥ 80, `👍 Worth considering` ≥ 70,
`🟡 Maybe` ≥ 60) are configurable.

### Two rules the engine will not break

1. **Unknown is never a claim.** If a posting does not mention sponsorship, the
   answer is `unknown` — never "available" and never "not available". Same for
   deadlines, salary, and experience requirements. Unknown values are surfaced
   as *concerns*, never used as silent filters.
2. **Only your explicit configuration excludes.** Blacklisted companies,
   excluded locations, and hard-exclude keywords are the only hard filters.
   Everything else is a ranking adjustment.

---

## Company discovery

The engine that makes the company list a preference rather than a boundary:

1. Aggregators, curated lists, and HN posts return listings.
2. Every URL is regex-mined for an ATS board identity — including
   **aggregator apply-links**, which is how an unknown employer is revealed.
3. New boards are registered and crawled **directly** on later runs, returning
   postings the original aggregator never indexed.
4. Those postings expose more boards, and the loop continues.

On a first seed run this yields **~550 crawlable ATS boards from ~770
companies**; a full pipeline run then discovers hundreds more.

Boards are crawled least-recently-first, so the whole registry is covered across
successive runs. Persistently failing boards back off but are never deleted.

---

## Notifications

Email (SMTP) and Telegram are the shipped channels; `NotificationProvider` makes
adding Discord, Pushover, or SMS a subclass. Console and file providers exist so
the path is testable before any credentials are set, and an unconfigured channel
degrades to the next configured one rather than losing the digest. See
[Deployment](#deployment) for the email setup.

A job reaches your phone only if it: scores at or above your minimum, has not
been notified before, has not been dismissed or applied to, and is still active.
Notification history is stored per job, so **a job moving from Indeed to
LinkedIn does not re-alert you** — only a material change does, and only after a
cooldown.

```
🚀 Internship Search — Aug 18

23 new internships found.
🔥 4 worth applying to
⭐ 7 strong matches

TOP MATCHES

1. NVIDIA — FPGA Design Intern
94/100 · Strong FPGA + SystemVerilog match
📍 Santa Clara, CA · 4 sources
```

The shipped GitHub Actions workflow pulls once daily at 12:17 UTC (8:17am
Eastern in summer, 7:17am in winter). Mail is sent only when a job clears your
threshold and has not been sent before. Change the cron in
`.github/workflows/digest.yml` to adjust the hosted schedule; dashboard schedule
settings apply only when the app's own scheduler is enabled.

---

## Accounts and sharing

One instance can serve several people. The expensive half — crawling hundreds of
boards, deduplicating, discovering employers — happens **once** and is shared;
everything subjective is per account.

| Shared | Per account |
|---|---|
| Jobs, listings, companies, ATS registry | Preferences, thresholds, weights |
| Search runs and coverage | Candidate profile and resumes |
| Deduplication | Status, tracker, notes |
| | Notification history and digest email |

The consequence that matters: **your friend marking a job "applied" does not
silence it for you.** Status lives in `user_job_state`, keyed by
`(user_id, job_id)`, because what you have done about a posting is an opinion,
not a fact about the posting. Rows are created only when someone makes a
decision, so the table stays proportional to decisions rather than to the
thousands of jobs crawled.

Adding someone costs one signup and almost no extra runtime: the same crawl
feeds their digest, scored against their own thresholds and sent to their own
address.

---

## Dashboard

| Page | What it does |
|---|---|
| **Jobs** | Canonical, deduplicated list. Filter by score, company, skill, source, location type, date, priority. Save / dismiss / mark-applied inline. |
| **Job detail** | Score breakdown, why-it-matches, why-you-might-skip, every source that carried it, resume recommendation, talking points, note-taking. |
| **Tracker** | Kanban: New → Saved → Applied → Assessment → Interview → Offer (plus Rejected). |
| **Coverage** | Per-source results **from actual runs**, source health, discovery totals, which sources produce strong matches. |
| **Analytics** | Dedup effectiveness, application funnel, outcome learning. |
| **Settings / Profile** | Every role, keyword, weight, threshold, and schedule. |

Filtering by source still returns **canonical jobs**, never one row per listing.

---

## Application assistant

`python -m app.cli apply` runs on your own machine and does everything up to
the Submit button:

1. Picks up to `daily_cap` (default 5) of the day's best open matches --
   `apply_now` or `strong_match`, not applied to or dismissed -- that have a
   Greenhouse form.
2. Reads each form's questions from Greenhouse's public board API, with the
   exact dropdown options.
3. Answers them: contact details and resume from your profile, your standing
   answers, rules for the questions every form asks (sponsorship, work
   authorization, school, degree, graduation, demographics declined), then the
   configured model for what is left -- told to use only your profile and
   resume, and to leave a question blank rather than guess.
4. Opens each form in Chrome, filled in with the resume attached, and lists
   anything the AI wrote and anything left for you. You review it and click
   Submit; on the confirmation page the job is marked applied in the tracker.
   Close the tab to skip a job.

Lever, Ashby and Workday forms are not filled; those jobs stay in the digest.

Setup, once:

```bash
pip install -e ".[apply]"
playwright install chromium
cp app/apply/profile.example.json data/apply/profile.json   # then fill it in
```

Everything personal -- profile, drafts, the browser profile -- lives in
`data/apply/`, which git ignores. Run it against the same database the digest
uses (Neon, for the GitHub Actions deployment) so it sees the same jobs and
the tracker stays in one place: put `DATABASE_URL` in `.env.apply`, also
ignored, and run `scripts/apply.ps1`, which loads it before calling the CLI.

```bash
python -m app.cli apply --prepare-only     # draft without opening anything
python -m app.cli apply --open-only        # open what is already drafted
python -m app.cli apply --job URL          # one specific Greenhouse link
python -m app.cli apply --job URL --check  # fill invisibly, screenshot, never submit
```

---

## Swipe to apply (cloud)

The **Swipe** tab shows one job at a time: the best undecided matches that have
a Greenhouse form. Swipe right (→) and an agent in the cloud fills the form and
submits it; swipe left (←) passes; **Z** undoes. Progress, what was sent, and a
screenshot of every form are on **Auto-apply status** (account menu).

```
 dashboard (Render)                 Neon Postgres                 GitHub Actions
 ──────────────────                 ─────────────                 ──────────────
 swipe right ──── ApplyTask ──────▶ apply_tasks  ◀── claim ─────  autoapply.yml
             └─ workflow_dispatch ─────────────────────────────▶  python -m app.cli autoapply
                                    applicant_records ── profile + resume ──▶ headless Chrome
                                                     ◀── result + screenshot ──  fills, submits
```

What the worker does per task, and where it stops:

| Step | Stops with "needs you" when… |
|---|---|
| Answer every question: profile → your standing answers → rules → Claude for the rest | a **required** question can't be answered from your profile or resume (never guessed) |
| Fill the form in headless Chrome | a required answer can't be placed on the page |
| Click Submit, wait for Greenhouse's confirmation | the form shows errors, a CAPTCHA challenge appears, or no confirmation comes |
| Greenhouse emails a security code | it has no IMAP access to your inbox, or the email doesn't arrive in 2 min |

Only a confirmed submission is marked **Applied** in the tracker. A closed
posting is marked failed. Handed-back tasks get **Open the form**, **I applied
myself** and **Try again** buttons. A daily ceiling (`AUTOAPPLY_DAILY_LIMIT`,
default 30) guards against a runaway queue.

Free-text answers are written by **Claude on your subscription** via
`claude -p` (Sonnet), told to use only your resume and facts and to decline
rather than invent. Groq (`LLM_API_KEY`) is the fallback.

### One-time setup

1. **Deploy**: push to `main`. Render redeploys and runs the migration.
2. **Upload your applicant profile** to the hosted database, once (and again
   after editing it). `.env.apply` holds the Neon `DATABASE_URL`, as for the
   application assistant:
   ```powershell
   .\scripts\push-profile.ps1
   ```
   Or paste the JSON and upload the PDF on the Auto-apply status page.
3. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `CLAUDE_CODE_OAUTH_TOKEN` — run `claude setup-token` and paste the token.
   - `DATABASE_URL`, `SMTP_*` — already there for the digest. The SMTP login is
     reused over IMAP to read Greenhouse security codes; set `APPLY_IMAP_HOST`,
     `APPLY_IMAP_USER`, `APPLY_IMAP_PASSWORD` if your applicant email is a
     different mailbox (for `@andrew.cmu.edu`, a Google app password on that
     account).
4. **Instant start** (optional but recommended): create a fine-grained GitHub
   token with *Actions: read and write* on this repository only and set it as
   `GH_DISPATCH_TOKEN` in the Render dashboard. Without it, the worker only
   runs once a day, right after the digest.

Test without sending anything: *Actions → Auto-apply → Run workflow* with
**dry run** ticked fills every queued form, stores the screenshot, and leaves
the tasks queued. Locally: `python -m app.cli autoapply --dry-run`.

### Fast dashboard

Render's free plan sleeps after 15 idle minutes and takes most of a minute to
wake. `.github/workflows/keepalive.yml` pings `/health` every ten minutes from
7am to 2am Eastern, so the dashboard opens instantly whenever you are likely to
use it. It needs the `PUBLIC_BASE_URL` secret. If you would rather pay a few
dollars a month for a guaranteed instant start, Fly.io with
`auto_stop_machines = "suspend"` resumes in well under a second.

### Usage budget (all free tiers)

Nothing here polls. Every cost is caused by you opening the dashboard, a swipe,
or the existing daily digest.

| Service | Limit | What this feature adds |
|---|---|---|
| **Neon** compute | scales to zero after 5 idle min | Worker runs only when you swiped (and once right after the digest, while Neon is already awake). The keep-warm ping never touches the database. |
| **Neon** storage (0.5 GB) | | Profile + resume ≈ 150 KB once. Per application: answers ≈ 3 KB; screenshot ≈ 50 KB (confirmation) to ≈ 250 KB (handed-back form), **deleted after 14 days**. |
| **Neon** egress (5 GB/mo) | | A deck of 10 cards reads card fields only (no descriptions, no crawler payloads, no resume) — a few KB. The status page never loads screenshots unless you click one. |
| **Render** (750 instance-h/mo) | | Kept awake 19 h/day ≈ 590 h/mo; sleeps overnight. |
| **GitHub Actions** | unmetered on public repos | ~2 min per worker run; keep-warm pings take seconds. |
| **Claude** subscription | your plan's usage window | One Sonnet call per application, only when a form has questions your profile doesn't answer. Hard cap `AUTOAPPLY_DAILY_LIMIT` (30/day). |

---

## Architecture

```
app/
├── config.py            settings (env-driven)
├── models/              SQLAlchemy: jobs, listings, companies, ats_boards,
│                        applications, notifications, search_runs, resumes
├── schemas/             Pydantic: preferences, profile, job DTOs
├── sources/             pluggable job sources
│   ├── base.py          JobSource / QueryJobSource / BoardJobSource
│   ├── http.py          retries, backoff, per-host rate limiting, caching
│   ├── ats/ boards/ lists/
│   └── registry.py
├── pipeline/
│   ├── identity.py      URL canonicalization + ATS identity extraction
│   ├── prefilter.py     cheap gate before expensive work
│   ├── normalize.py     RawJob → NormalizedJob
│   ├── extract.py       sponsorship, salary, deadlines, locations
│   ├── dedupe.py        5-stage dedup + guards + union-find
│   ├── match.py         transparent weighted scoring
│   ├── queries.py       dynamic query generation + expansion
│   ├── discovery.py     company + ATS board discovery
│   ├── llm.py           optional semantic stages
│   └── runner.py        orchestration
├── services/            persistence, actions, resumes, analytics, learning
├── notify/              providers, digest building, dispatch rules
├── web/                 FastAPI routes + Jinja2/HTMX templates
└── scheduler.py         APScheduler cron
```

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic v2 ·
httpx · APScheduler · SQLite (dev) / PostgreSQL (prod).

**One deliberate deviation from the brief:** the frontend is server-rendered
Jinja2 + HTMX rather than Next.js. For a single-user dashboard that is mostly
lists, filters, and four action buttons, this keeps the whole system to one
process, one port, and one deploy, with no Node runtime or CORS surface. The
REST API under `/api/*` is complete and frontend-agnostic, so a React frontend
can be added later against the same endpoints without touching the backend.

---

## CLI

```bash
python -m app.cli search               # full pipeline + notification
python -m app.cli search --no-notify --dry-run
python -m app.cli seed                 # seed companies/boards from curated lists
python -m app.cli notify-test --provider telegram
python -m app.cli stats                # what is in the database
python -m app.cli serve --reload
python -m app.cli apply                # open today's applications filled in; you submit
```

---

## Configuration

All settings are editable in the UI and stored as a validated document; `.env`
only holds secrets and deployment settings. See `.env.example` — every key is
optional except `DATABASE_URL`, which has a working default.

For PostgreSQL — local, or a free hosted one, see [Deployment](#deployment):

```bash
# A provider's own `postgresql://...` string is accepted as pasted; the driver
# is filled in for you.
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/internship
pip install -e ".[postgres]"
python -m alembic upgrade head
```

---

## Deployment

The digest has to arrive whether or not any machine of yours is switched on, so
something has to run daily and remember what it already sent you. There
are two shapes of that, and the difference is only *where the clock lives*.

| | Free option | Always-on option |
|---|---|---|
| Schedule | GitHub Actions cron | the app's own APScheduler |
| State | hosted Postgres (Neon free tier) | SQLite on a persistent volume |
| Dashboard | run locally against the same database | public URL, 24/7 |
| Cost | **$0** | Fly.io Hobby plan minimum, ~$5/month |

Both send the identical email. Start free; the always-on setup is a drop-in
upgrade later, and the database can move with you.

### Email digests (needed by both)

The `email` provider sends the digest as a multipart HTML email over plain SMTP,
so any mail account works. With Gmail:

1. Turn on 2-factor auth on your Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>
   (a normal account password **will** be rejected).
3. Fill in `.env`:

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop     # the 16-char app password, spaces removed
EMAIL_TO=you@andrew.cmu.edu        # comma-separated for multiple inboxes
```

4. Verify, then select the channel in **Settings → Notifications**:

```bash
python -m app.cli notify-test --provider email
```

Every digest is `multipart/alternative`, so a client that refuses HTML still
gets the full text version. All digests share a `References` header, so Gmail
threads them into one conversation rather than two new emails a day.

If the configured provider is not set up, the engine degrades in order —
email → Telegram → `data/notifications.jsonl` — so a digest is never lost.

---

### Option A — free: GitHub Actions + Neon Postgres

Nothing is always-on, so nothing is billed. Actions supplies the schedule and
the compute; a free hosted Postgres supplies the memory between runs, which is
what makes "do not tell me about this job twice" work across runs that share no
filesystem.

**1. A database that outlives the runner.** Create a free project at
<https://neon.tech> (no card) and copy the connection string. Paste it exactly
as given — `postgresql://…` is rewritten to the driver SQLAlchemy needs.

> Neon over Supabase here: Supabase pauses free projects after a week of
> inactivity, which is exactly the failure mode a twice-daily job would hit.

**2. Push the repository to GitHub**, then add these under
*Settings → Secrets and variables → Actions*:

| Secret | Value |
|---|---|
| `DATABASE_URL` | the Neon connection string |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | where the digest should land |

Any Tier-4 API keys you have can be added as secrets with the same names as in
`.env`; the workflow passes them through, and absent ones simply leave that
source unconfigured.

**3. Run it once by hand** — *Actions → Internship digest → Run workflow* — to
migrate the database and prove the email arrives. The first run is the slow one:
it seeds the registry from the curated lists.

That is the whole setup. `.github/workflows/digest.yml` then fires on its own.

**The schedule is once a day**, at 12:17 UTC. This halves scheduled pulls
compared with twice daily, reducing database compute and transfer. New postings
can take up to a day to appear. Use Actions ? Run workflow for an extra manual
pull when needed. The dashboard reads the saved results on demand; it does not
need to stay awake for the daily pull. Listing identity lookups omit unused raw
payloads to reduce repeated database downloads.

Render also sets `BOOTSTRAP_SEED_ENABLED=false`, so waking the dashboard does
not start registry seeding. Daily searches populate the registry.

**The dashboard.** Two ways, and they can coexist:

*Locally* — point your `.env` at the same `DATABASE_URL` and run
`python -m app.cli serve`. You see exactly what the scheduled runs produced,
and anything you save, dismiss or mark applied is respected by the next run.
Set `SCHEDULER_ENABLED=false` locally so your laptop does not also send digests.

*Hosted, also free* — `render.yaml` deploys the dashboard to Render's free
plan. This works precisely because the service is now stateless: the database
is in Neon and the schedule is in Actions, so the dashboard holds nothing of
its own and can sleep, restart or be rebuilt without losing anything or missing
a digest.

1. At <https://render.com>, *New → Blueprint*, point it at this repository.
2. Set the one secret it asks for: `DATABASE_URL` (the Neon string).
3. Once it is live, add the URL as a **`PUBLIC_BASE_URL` GitHub secret**, so
   the "View the full dashboard" link in each digest points at it instead of
   falling back to `localhost`.

The free plan sleeps after 15 minutes of inactivity, so the first page load
after a quiet spell takes 30–60 seconds while it wakes. For a dashboard opened
a couple of times a day that is the right trade for $0; nothing about the
digests depends on it being awake.

**Two honest caveats.**

- GitHub **disables scheduled workflows after 60 days without repository
  activity**, and emails you when it does. Any commit re-enables them.
- Scheduled runs are best-effort and queue behind GitHub's load; a digest can
  land a few minutes late. It has never mattered for a job posting.

Free minutes are not a concern: public repositories get unlimited Actions
minutes, and a private repository's 2,000 free minutes comfortably cover two
runs a day.

---

### Option B — always-on: Fly.io

`Dockerfile`, `docker-entrypoint.sh` and `fly.toml` are checked in. The entrypoint
runs migrations and then serves. Seeding the ATS registry takes minutes, so it is
**not** done before the port binds — the app seeds itself in the background on
first boot, deciding from the registry's own contents whether seeding is needed.

```bash
# Install flyctl, then:
fly auth signup

# Pick a unique name; --no-deploy so secrets can be set before the first boot.
fly launch --no-deploy --name <your-app-name>

# 1GB persistent volume for SQLite, resumes and the HTTP cache.
fly volumes create internship_data --size 1 --region ewr

fly secrets set \
  SMTP_HOST=smtp.gmail.com \
  SMTP_USER=you@gmail.com \
  SMTP_PASSWORD='your-app-password' \
  EMAIL_TO=you@andrew.cmu.edu \
\
  PUBLIC_BASE_URL=https://<your-app-name>.fly.dev

fly deploy
fly logs                    # watch the migration, scheduler start, and seed
```

Then open `https://<your-app-name>.fly.dev`, create your account, and set
**Notifications → Provider** to `email`.

Two settings in `fly.toml` are load-bearing:

- `auto_stop_machines = false` and `min_machines_running = 1`. A scheduler that
  is asleep at 08:00 does not send anything, so the machine is never suspended.
- `memory = "512mb"`. A pipeline pass over thousands of listings will OOM at
  256MB, even though serving the dashboard would not.

**On cost:** Fly withdrew its free allowance for organisations created after
late 2024, and its Hobby plan carries a $5/month minimum. This app's footprint
sits under that minimum, so the practical cost is the plan minimum, not zero.
An older organisation that still has the free allowance runs it for nothing.

To use hosted Postgres here too, set `DATABASE_URL` and drop the `[[mounts]]`
block; the image already installs the `postgres` extra.

---

### Accounts

The dashboard has real accounts: scrypt-hashed passwords and an HMAC-signed
session cookie, both stdlib, no session store. Every page and every `/api`
route requires one; only `/health` and the sign-in pages are open, because the
platform's health check runs unauthenticated.

The signing key is persisted in the database rather than generated per process,
because a free host stops the web service whenever it is idle and a
process-local key would log everyone out several times a day.

**Upgrading an instance that predates accounts:** the first person to sign up
adopts the existing `me@localhost` row, keeping its tracker, preferences and
profile. That only happens while no account has a password, so an established
instance can never be claimed by a stranger. Everyone who signs up afterwards
gets their own account.

---

## Testing

```bash
python -m pytest              # 287 tests
python -m ruff check app tests
```

Coverage spans ingestion and malformed listings, all five dedup stages and every
anti-over-merge guard, scoring and eligibility, notification rules and repeat
suppression, scheduling and timezones, source adapters against recorded payloads
(no network), and the HTTP client's retry/backoff behaviour.

---

## Honest limitations

- **Coverage is partial by construction.** No system sees every internship on
  the internet. The dashboard reports what was actually searched, and a source
  that failed or was unconfigured is labelled as such rather than counted.
- **Workday** exposes an undocumented (though public and stable) endpoint;
  tenants that expose no intern facet fall back to relevance-ranked search,
  which reads only the top pages.
- **Hacker News** comments are free-form; parsing is conservative and will miss
  unconventional formats.
- **Deadline and salary extraction** are best-effort over free text. Absence is
  always reported as unknown, never guessed.
- **Curated lists** reflect their maintainers' scope and freshness.
- Resume text extraction from PDFs has no OCR; scanned resumes yield no text.

## Not implemented (deliberately)

Submitting applications for you. Every Greenhouse, Lever and Ashby form sits
behind a bot check (Greenhouse loads reCAPTCHA Enterprise on every board), and
an application goes out under your name. The application assistant below fills
forms in; you always click Submit. Resume recommendations never rewrite your
resume or invent experience.
