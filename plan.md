# What to do next

Written 22 Aug 2026, after the ranking rewrite landed on `main` (`d89dcbe`).
Every number below is measured against `data/internship.db` as of that date —
5,134 active jobs, one account.

---

## Where things stand

The ranker was rewritten and the corpus re-scored. The funnel is usable again:

| Band | Threshold | Jobs | Before the rewrite |
|---|---|---|---|
| 🔥 Apply now | ≥ 90 | 3 | 1 |
| ⭐ Strong match | ≥ 80 | 41 | 3 |
| 👍 Worth considering | ≥ 70 | 66 | 44 |
| 🟡 Maybe | ≥ 60 | 30 | 256 |
| Skip | < 60 | 4,994 | 4,830 |

110 jobs now clear 70, against 48 before. That is the number that matters — it
is the size of the pile actually worth reading.

**Three things are built but not yet doing anything:**

1. **Enrichment has never run.** `LLM_ENABLED` is false and no model is
   configured, so 0 postings have been read. 2,626 active jobs still have no
   description and score nothing on skills.
2. **Board yield is unpopulated.** The bookkeeping fix landed, but the last
   crawl was 19 Aug — before it existed. All 615 boards still report 0.
3. **The deployed instance has not been migrated or re-scored.** Only the local
   database is on the new scale.

---

## 1. Land what already exists

**Nothing new gets built until the work already on `main` is actually running.**

### 1a. Deploy

```bash
# migration f5a6b7c8d9e0 adds the four enrichment columns
alembic upgrade head
internship-search rescore
```

`docker-entrypoint.sh` should run the migration, but that path has only ever
been exercised locally. The rescore is not optional: a corpus split across two
scoring generations makes every threshold meaningless, which is the exact
failure the rewrite was fixing.

**Verify:** the deployed feed's top card is a hardware internship, and
`/coverage` reports the same run counts as local.

### 1b. Run a crawl

The last one was three days ago, and it predates every change. It is also the
only way to populate board yield.

```bash
internship-search search --trigger manual
```

**Verify:** `ats_boards.jobs_last_crawl > 0` for a reasonable share of boards.
Any board that succeeds and returns zero is now visible, which is what the
AMD gap needs.

**Watch for:** the scheduler may not be running at all — nothing has run since
19 Aug despite being configured for twice daily. If the crawl only happens when
triggered by hand, that is a separate bug worth chasing.

---

## 2. Turn on the model

This is the highest-value unproven work. It is written, tested against a stub,
and has produced exactly **zero real inferences**.

```bash
ollama pull qwen2.5:7b
# .env
LLM_ENABLED=true
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b

internship-search llm-check          # proves the endpoint answers
internship-search enrich --limit 20  # small batch first, read the output
internship-search enrich --rescore   # full pass: ~322 calls
```

Start at `--limit 20` and **actually read what it extracted** before running the
full pass. The selector picks 322 of 5,134 jobs (6.3%), so a full pass is cheap,
but a 7B model writing plausible nonsense into 322 job records is not something
to discover afterwards.

**Verify:** pick five enriched jobs, compare `enrichment.skills` against the
posting text by eye. Then check the ranking moved sensibly — not just that it
moved.

**Honest risk:** the projected benefit is unmeasured. 63% of postings yield no
skills today, but the ones the selector picks are the ones whose titles already
match, and those may already score well on `role_match` alone. The lift could be
smaller than the plan implies. Measure it before committing to a hosted model or
a scheduled backfill.

**If the local model is too slow or too poor:** point `LLM_BASE_URL` at Groq's
free tier with a `LLM_API_KEY`. Same code path, bigger model, rate-limited.

---

## 3. Recalibrate thresholds — but *after* step 2, not before

The distribution has room to move:

```
≥90:   3     ≥75:  76
≥85:  19     ≥70: 110
≥80:  44     ≥65: 124
```

Three jobs at "apply now" makes a thin headline. Dropping `apply_now` to 85 and
`strong_match` to 75 would give 19 and 76 — a more usable shape.

**But do not touch these yet.** Enrichment changes scores for exactly the jobs
near the top, so tuning now means tuning twice and having no idea which change
did what. Re-read the distribution after step 2 and set the thresholds once.

---

## 4. Fix the AMD gap

AMD has **one** active job and it is not an internship. For comparison:

| Company | Jobs | Internships |
|---|---|---|
| NVIDIA | 32 | 32 |
| Apple | 13 | 13 |
| Microsoft | 10 | 10 |
| Google | 5 | 5 |
| **AMD** | **1** | **1** |

So it is not a systemic board problem — it is AMD specifically. No ranking change
can surface a job that was never crawled.

Find AMD's real Workday tenant and site identifier from a live posting URL, then
register the board. The `/coverage` page now warns about preferred companies
with no internships, so this class of gap reports itself from here on.

**Also worth checking while in there:** Google at 5 and Microsoft at 10 look low
for employers that run hundreds of intern reqs. They may be paginating, or the
title gate may be dropping them.

---

## 5. Start capturing decision signal

From `LEARNING-LOOP` (artifact): you have **1 application, 0 saves, 0 dismissals,
0 recorded apply-clicks**. Nothing can learn from that, and signal not captured
now is gone.

This is the only part of the learning plan worth building before there is data,
because everything else depends on it.

- **One-tap dismissal reasons.** Wrong role · Wrong location · Too senior ·
  Won't sponsor · Not this company · Bad timing. Optional, one tap, never a
  dialog that interrupts triage. A dismissal without a reason is nearly
  unlearnable — it could mean any of six things that imply opposite preference
  changes.
- **Snapshot the score on save and dismiss**, not just on apply.
  `Application.score_at_apply` already does this for applications (2 of 2
  populated); the other two decisions need the same.
- **Log what was shown.** Top-*k* job ids per feed impression. Without a
  denominator there is no precision@k, and precision@k is the only honest
  measure of whether any of this is working.

Small schema change, a chip row on the card. Do it before the learning layers,
not with them.

---

## 6. Feed clutter

**320 `(company, title)` groups have more than one card in the review feed.**
These are not duplicates — I checked requisition IDs, and they are genuinely
separate openings sharing one boilerplate description. GE Healthcare advertises
11 distinct "Graduate Engineer Trainee" reqs; Texas A&M 14 work-study reqs.
Merging them would delete real jobs.

The fix is presentational: collapse same-employer, same-title postings into one
card that expands to show the individual reqs. Roughly the same shape as the
existing "Merged from N listings" badge, but for jobs that were correctly *not*
merged.

Lower priority than everything above — it is noise, not wrongness.

---

## 7. Learning layers 1–3

Deferred until step 5 has produced ~30 decisions. Full design in the
**Learning From Your Decisions** artifact. Summary of the order:

- **Layer 1** — smoothed log-odds per feature over applied vs dismissed. No
  model. Gated on a confidence floor, emits *proposals* rather than mutating
  scores.
- **Layer 3** — the proposal review queue. Ship with Layer 1; a proposal with
  nowhere to land is not useful.
- **Layer 2b/2c** — weekly model call to name the pattern and expand role
  vocabulary. Needs ~100 decisions before it is summarising anything real.

The constraint that matters most is in that artifact and bears repeating here:
**never let a learned preference drive a hard filter.** A filter removes a job
from the feed, which removes it from the evidence, which closes the loop
permanently — and the loop would have closed around a ranker that was badly
wrong until this week.

---

## Open questions

- **Is the scheduler running?** Nothing has crawled since 19 Aug. Either it is
  not started in the deployed process, or it is failing silently.
- **Notifications are off.** The channel guard disabled them because Telegram
  has no bot token. Either finish the Telegram setup or switch to email — until
  then the digest does nothing regardless of how good the ranking is.
- **Does `role_affinity` need per-role tuning?** "Hardware Verification Intern"
  scores ~65 because it shares one meaningful token with each of two roles.
  Adding it as an explicit role fixes it, which may be the right answer rather
  than more clever matching.
- **`MIN_ROLE_AFFINITY = 0.35` is a guess.** It picks 6.3% of the corpus for
  enrichment. If the model turns out to be cheap and useful, lowering it widens
  the net.

## Explicitly not doing

- **Scoring jobs with an LLM.** Slow, non-deterministic, unexplainable, and
  unnecessary now the deterministic scorer works. The model's output belongs in
  a field the scorer reads, or a proposal you approve — never in a score.
- **Merging the 315 same-title/different-requisition postings.** They are real,
  separate jobs. Group them in the UI instead.
- **Auto-applying learned preference changes.** Not until several proposals have
  been accepted without regret, and never for hard filters.
