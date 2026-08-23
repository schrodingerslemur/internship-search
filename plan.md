# What to do next

Updated 23 Aug 2026, after working through the previous version of this plan.
Numbers are measured against `data/internship.db` — 5,262 active jobs, one
account.

---

## Where things stand

| Band | Threshold | Jobs | Two plans ago |
|---|---|---|---|
| 🔥 Apply now | ≥ 85 | 29 | 3 |
| ⭐ Strong match | ≥ 75 | 61 | 41 |
| 👍 Worth considering | ≥ 70 | 47 | 66 |
| 🟡 Maybe | ≥ 60 | 31 | 30 |
| Skip | < 60 | 5,094 | 4,994 |

The top ten are all AMD and NVIDIA hardware internships. That is the intended
shape, and it is the first time it has been true.

**Three corrections to the previous plan, all worth recording:**

- **The scheduler was never broken.** It runs in GitHub Actions every three
  hours and has been succeeding continuously. What looked like a dead scheduler
  was the *local* database being a different database from the deployed one.
- **AMD is not on Workday.** Its careers site is Phenom People, which is why
  every guessed Workday tenant returned 422.
- **Enrichment does not simply raise scores.** Measured over 112 postings: 25
  went up, 22 went down, mean −2.1. See below — the drops are the point.

---

## Done

**Board yield populated.** 664 boards crawled, 443 yielding, 220 succeeding but
returning nothing — that last number was invisible before.

**The AMD gap is closed.** A `phenom` source, AMD registered as a curated board,
and `seed_curated_boards` at the head of every crawl so a board added in a
release reaches an already-seeded database. **AMD: 1 job → 43.**

**Workable was entirely dead.** It sends `department` as a list; the schema
wants a string. The validation error killed the posting, then the board, then
every board, then the source, reported only as `[FAIL] workable 0`.

**Decision signal is captured** (migration `a6b7c8d9e0f1`): one-tap dismissal
reasons in the undo toast, score snapshots on save and dismiss, and a
`feed_impressions` log of what each feed showed. None of it is retroactive,
which is why it landed before the layers that consume it.

**The feed says a role once.** 318 (company, title) groups were costing 525
extra cards, collapsed presentationally with every requisition still linked and
individually applicable.

**Thresholds set to 85 / 75**, and re-checked after enrichment: the boundaries
barely moved (30 at ≥85, 90 at ≥75, both within one of their pre-enrichment
values), because enrichment redistributes within the corpus rather than
shifting its shape.

**The model is on**, and turning it on found five bugs a stub could not:

1. `.env` named an Anthropic model against Ollama's base URL — a combination
   nothing could answer. That is why enrichment had produced zero inferences.
2. Reasoning models spend the answer's token ceiling *thinking first*, so the
   32-token health probe and 700-token extraction budget were consumed before
   any JSON appeared. It surfaces as `"failed_generation": ""`, which looks
   nothing like a token limit.
3. 429 was treated as failure — reported as "did not return usable JSON", a
   much more alarming problem, *and* already charged to the run budget.
4. The backfill held every posting in one transaction and committed at the end.
   An interruption discarded every posting already read along with the quota
   spent on it. It did, and about a hundred postings went with it.
5. One extraction in six failed on long postings for reason (2) again; those
   are now retried with three times the room, rather than raising the ceiling
   for everything — the requested ceiling counts against the per-minute rate
   limit whether or not it is used.

### What enrichment actually bought

Over the 112 postings read so far:

| | Before | After |
|---|---|---|
| Average skills per posting | 2.8 | **16.2** |
| Postings scoring *zero* on skills | 27% | 0 |
| Extracted skills appearing verbatim in the posting | — | **97.6%** |

The score movement is the interesting part, and it is not what the previous
plan assumed. **Epia Neuro's "Hardware Engineer Intern" fell from 84.8 to
40.9.** It had no extractable skills, so `technical_skills` was *unmeasured* and
silently dropped out of the weighted average — the posting scored 84.8 on its
title alone. Reading it found `solidworks, fdm 3d printing, hand tools, drill
press, sanding, filing, adhesives`: a mechanical fabrication internship with no
overlap with an FPGA/RTL profile. Three more sat at ~84 for the same reason.

Going the other way, Astera Labs' Firmware Engineer Intern rose 35.5 → 69.7 on
`c, c++, python, git, ci, jtag`, and DRW's FPGA Intern 50.4 → 72.9 on
`system verilog, verilog, vhdl`.

So enrichment is not a boost, it is a **correction**: it takes away scores that
were never earned and gives them to postings the vocabulary could not read. An
unmeasured component quietly inflating a score is worth looking at on its own
terms — it is currently the most generous thing the scorer does.

---

## 1. Finish the backfill — 112 of 332, and it needs several days

Groq's free tier is **8,000 tokens/minute and 200,000 tokens/day**, and the
daily cap is **per model**, not per account. All three usable models are spent
for today.

```bash
internship-search enrich --limit 400   # resumes where it stopped; safe to interrupt
```

Re-run daily until it reports no remaining eligible postings. Rotating
`LLM_MODEL` between `openai/gpt-oss-20b`, `qwen/qwen3.6-27b` and
`openai/gpt-oss-120b` gets three daily allowances instead of one.

Cost per posting varies more than the model sizes suggest: `gpt-oss-20b` uses
about 500 tokens, `gpt-oss-120b` about 2,000, and `qwen3.6-27b` burns ~970
tokens *thinking* before it answers. **`gpt-oss-20b` is both the cheapest and
the best of the three here** — 166 of 170 of its extracted skills appear
verbatim in the posting, cleanly lowercased.

**Then re-read the distribution once more.** The thresholds are set on a corpus
that is 34% enriched.

## 2. Enrichment only runs locally

The GitHub Actions workflow configures no LLM, so the deployed instance enriches
nothing. Either add `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_ENABLED` as repository
secrets, or accept that enrichment is a local backfill and that only its
*results* reach production, through the shared database.

## 3. Google, Microsoft and Apple have the same gap AMD had

Checked, as the previous plan asked. It is **not** pagination and **not** the
title gate:

| Company | Registered boards | Jobs | Only source |
|---|---|---|---|
| Google | **0** | 5 | `github_lists` |
| Microsoft | **0** | 12 | `github_lists` |
| Apple | **0** | 13 | `github_lists` |
| NVIDIA | 1 (workday) | 37 | workday + lists |

Every one of their jobs arrives second-hand from curated lists. None answers a
trivially-guessable endpoint — Microsoft's `gcsservices` search returned empty,
Apple's redirects, Google's 404s — so each needs its own adapter, the way
Phenom did. Real work, not a registration.

## 4. Learning layers 1–3

Still gated on **~30 decisions** existing; there are 2 applications, 0 dismissal
reasons and 2 impressions on record. The capture is in place, so this waits on
use rather than on code. Full design in the **Learning From Your Decisions**
artifact.

The constraint that matters most bears repeating: **never let a learned
preference drive a hard filter.** A filter removes a job from the feed, which
removes it from the evidence, which closes the loop permanently.

---

## Open questions

- **Should an unmeasured component really be free?** Excluding
  `technical_skills` from the weighted average when nothing was extracted is
  what let a 3D-printing internship reach 84.8. Enrichment fixes this one
  posting at a time; the scoring rule behind it is untouched and applies to
  2,660 active jobs that still have no description at all.
- **Does `role_affinity` need per-role tuning?** "Hardware Verification Intern"
  scores ~65 because it shares one meaningful token with each of two roles.
- **`MIN_ROLE_AFFINITY = 0.35` is still a guess** — it selects 332 of 5,262
  (6.3%). Lowering it widens the net, at a daily quota that is already the
  binding constraint.
- **`feed_impressions` grows one row per feed render.** Fine for one user;
  worth a retention sweep before it is ever multi-user.
- **AMD appears under two display names** — both correctly resolve to one
  company row, so ranking and grouping are unaffected. Cosmetic only.

## Explicitly not doing

- **Scoring jobs with an LLM.** Unchanged, and the reason
  `scope.llm_semantic_matching` is deliberately still off: it feeds a model
  assessment into the score. The model's output belongs in a field the scorer
  reads, or a proposal you approve — never in a score. Enrichment is exactly
  that shape, and the Epia Neuro correction is what it looks like working.
- **`scope.llm_dedup_adjudication`, for now.** Not a rejection: it shares the
  per-run call budget with enrichment, which is already quota-bound.
- **Merging the same-title/different-requisition postings.** They are real,
  separate jobs. Grouped in the UI instead — done.
- **Auto-applying learned preference changes.** Not until several proposals have
  been accepted without regret, and never for hard filters.
