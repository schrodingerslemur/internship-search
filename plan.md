# What to do next

Updated 23 Aug 2026. Numbers measured against `data/internship.db` — 5,276
active jobs, one account.

---

## Where things stand

| Band | Threshold | Jobs |
|---|---|---|
| 🔥 Apply now | ≥ 85 | 18 |
| ⭐ Strong match | ≥ 75 | 51 |
| 👍 Worth considering | ≥ 70 | 35 |
| 🟡 Maybe | ≥ 60 | 65 |
| Skip | < 60 | 5,107 |

The apply-now band is smaller than it was yesterday (18, from 31) and that is
the point: 12 of the 18 now have a description that was actually read. The top
of the feed is AMD's verified hardware internships rather than AMD listings
nothing was known about.

### Preferred employers

| Company | Jobs | ≥ 75 | Reached via |
|---|---|---|---|
| AMD | 43 | 19 | phenom + lists |
| NVIDIA | 37 | 8 | workday + lists |
| Apple | 22 | 1 | **apple_jobs** + lists |
| Microsoft | 16 | 0 | **eightfold** + lists |
| Google | 5 | 0 | lists only |

AMD was 1 job two days ago. Apple was 13, Microsoft 12.

---

## Done since the last plan

**All three missing employers now have boards.** The previous plan guessed at
pagination or the title gate; it was neither. None of the three was on an ATS
this project crawled, and none answers a guessable endpoint.

- **Microsoft** was a dead end twice: `gcsservices.careers.microsoft.com` now
  serves a certificate for `*.azureedge.net` and 404s regardless, and
  `jobs.careers.microsoft.com` redirects to a new platform. Watching what that
  page requests found Eightfold, which has many large tenants — so the adapter
  is written against the platform, not against Microsoft.
- **Apple** serves a real search API, once given the *whole* filter object. A
  partial one is accepted and silently returns nothing.
- **Google** serves nothing machine-readable — no API, no JobPosting data,
  everything assembled in the browser. It does publish a sitemap whose URLs
  carry a job id and a title slug, which is one request for the whole employer
  and yields a title and a link. Deliberately not padded out with invented
  locations or dates.

**A posting we knew nothing about was outranking one we had checked.** The
scorer drops an unmeasured component rather than letting it vote with an
invented midpoint, which is right — but that also made missing evidence *free*.
A description-less posting has only `role_match`, that one component carried
the whole relevance group, and a title alone could score 100. AMD's
`Hardware Engineer Intern/Co-op` (no description) scored 96.1 and outranked
AMD's own `2027 Masters Hardware Engineering` at 94.6, whose 3,900 words had
been read and found to name verilog, vhdl, asic and jtag.

Relevance built on less evidence is now discounted. Jobs carrying real evidence
moved by **exactly zero**; title-only jobs by −0.5 on average, −6.7 at worst.

**Three copies of the same helper folded into one.** Workable sends
`department` as a list, Eightfold sends `employment_type` as one, Phenom sends
`category` as a padded list. Passing any through raises inside `make_job`,
which fails the posting, then the board, then the source. It had already cost
two sources before it was worth sharing.

---

## 1. Finish the backfill — 148 of 332

Groq's free tier is **8,000 tokens/minute and 200,000 tokens/day**, and the
daily cap is **per model**, not per account. All three usable models are spent
for today.

```bash
internship-search enrich --limit 400   # resumes where it stopped; safe to interrupt
```

Rotating `LLM_MODEL` between `openai/gpt-oss-20b`, `qwen/qwen3.6-27b` and
`openai/gpt-oss-120b` gets three daily allowances instead of one. **`gpt-oss-20b`
is both the cheapest and the best of the three** — about 500 tokens a posting
against 2,000 for the 120b, and 166 of 170 of its extracted skills appeared
verbatim in the posting. It is the configured default.

**Then re-read the distribution.** The thresholds are set on a corpus that is
45% enriched.

## 2. Enrichment still only runs locally

The GitHub Actions workflow configures no LLM, so the deployed instance enriches
nothing. Its *results* reach production through the shared database, so the
local backfill is worth continuing either way. Running it server-side means
adding `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_ENABLED` as repository secrets —
a decision about where the key lives, not a code change.

## 3. Google is reached but barely

Its 20 sitemap postings are titles only, and most are "Student Researcher",
which matches no target role and scores 0. The ones that *do* match a role now
score honestly rather than at 90, so nothing is inflated — but nothing much is
gained either. Google publishes no description anywhere machine-readable, so
this is close to the ceiling for that employer without a paid aggregator.

## 4. Learning layers 1–3

Still gated on **~30 decisions**: there are 2 applications, 0 dismissal reasons
and 2 impressions on record. The capture is in place, so this waits on use
rather than on code. Full design in the **Learning From Your Decisions**
artifact.

**Never let a learned preference drive a hard filter.** A filter removes a job
from the feed, which removes it from the evidence, which closes the loop
permanently.

---

## Open questions

- **The credentialed aggregators are all unconfigured** — Adzuna, JSearch,
  USAJOBS, Jooble, SerpApi. Adzuna's free tier in particular would cover Google
  and Microsoft far better than a bespoke adapter can, and the code is already
  written. This is now the cheapest remaining coverage win.
- **`EVIDENCE_DISCOUNT = 0.93` is a judgement, not a measurement.** It was
  chosen so a verified match outranks an unverified one with the same title,
  and checked against the corpus. If enrichment closes the description gap, it
  matters less each week.
- **Does `role_affinity` need per-role tuning?** "Hardware Verification Intern"
  scores ~65 because it shares one meaningful token with each of two roles.
- **`MIN_ROLE_AFFINITY = 0.35` is still a guess** — it selects 332 of 5,276
  (6.3%). Lowering it widens the net against a daily quota that is already the
  binding constraint.
- **`feed_impressions` grows one row per feed render.** Fine for one user;
  worth a retention sweep before it is ever multi-user.

## Explicitly not doing

- **Scoring jobs with an LLM.** Unchanged, and the reason
  `scope.llm_semantic_matching` is deliberately still off: it feeds a model
  assessment into the score. The model's output belongs in a field the scorer
  reads, or a proposal you approve — never in a score.
- **`scope.llm_dedup_adjudication`, for now.** It shares the per-run call
  budget with enrichment, which is already quota-bound.
- **Merging same-title/different-requisition postings.** They are real,
  separate jobs. Grouped in the UI instead — done.
- **Auto-applying learned preference changes.** Not until several proposals
  have been accepted without regret, and never for hard filters.
