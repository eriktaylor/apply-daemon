# Writing a profile that ranks well

`my_profile/profile.md` is the single input that decides what the pipeline
shows you. This document is about writing it well. Start from
`my_profile_example/profile.md`, which demonstrates the structure below.

Everything here is a *class* of defect observed in real profiles, with why the
pipeline punishes it and what to write instead.

---

## What actually reads your profile

One stage. Stage 5 scoring (`src/triage.py`) receives the whole file above
`## Pipeline Settings` and returns, per listing, a `verdict` (YES/MAYBE/NO)
and a `confidence` (0–100).

It is worth being precise about this, because it is easy to assume otherwise:

| Stage | Model tier | Sees your profile? |
|---|---|---|
| Stage 1 extraction (`_EXTRACT_PROMPT`) | nano | **No** — pulls listings out of email text. Track B only. |
| Stage 3 scrape/heal | — | No |
| **Stage 5 scoring** | flash / frontier | **Yes — the whole thing** |
| Mismatch gate | nano | **No** — checks the company name against the page; runs *after* Stage 5, inside autopilot |
| Post-research re-score | frontier | Job description + research dossier, plus profile |

So there is no cheap pre-filter applying your reject rules. Every rule you
write is executed by the same model, on the same pass, with the whole profile
in context.

## The two numbers that decide what survives

```
Stage 5 scores ──►  confidence < CONFIDENCE_THRESHOLD  ──►  DELETED. Not stored.
                                                            Never reviewable.
               └─►  survives  ──►  confidence < NOISE_FLOOR_PCT ──►  stored,
                                   reviewable, just not enriched by autopilot
               └─►  above both  ──►  ranked, top-N enriched, shown
```

**`CONFIDENCE_THRESHOLD` deletes. `NOISE_FLOOR_PCT` only declines to spend.**
They default to the same value, and for a ranked profile that is the wrong
setting: everything you deliberately rank low is discarded instead of ranked
low. Set the delete threshold low (`0.40`) and the enrichment floor higher
(`55`).

A `NO` verdict is always rejected regardless of confidence.

---

## 1. Write for rank order, not for a threshold

Selection among survivors is by **rank position within a scoring batch**, not
by absolute score. That was a deliberate choice: batch composition alone was
measured moving 6 of 24 listings across a fixed threshold, so absolute scores
are unstable in a way relative order is not.

A profile written for a *threshold* optimizes for the wrong thing. Threshold
thinking says "make sure good jobs clear the bar," which produces
score-inflating instructions. Rank thinking says "help the judge tell two
acceptable jobs apart," which produces discriminating criteria.

If your profile pushes most listings into the same band, the ranking among
them is noise — and the instability the design was meant to remove comes back
in through your profile.

Open your scoring section with something like:

> Selection is by rank order within a batch. Spread the scores. If most of a
> batch lands in the same band, the ranking carries no information. Given two
> listings that both look acceptable, say which is better and why.

**But respect the floor.** "Spread the scores" plus a high
`CONFIDENCE_THRESHOLD` means the spread you asked for gets deleted. Ranking
and deletion are different mechanisms; keep the delete threshold below the
bottom of your ladder.

## 2. Don't write score-inflation language

The most common defect, and it comes from an understandable place — the author
answers "would I take this job?", for which the honest answer is often "yes,
all of these." But the scorer is asking a different question: *which of these
forty should he see first?*

Phrases to search for and remove:

| Instruction | Effect under rank order |
|---|---|
| "these three families fit equally well — score as strong matches" | Collapses three large categories into one band |
| "bridge titles — score the same as target titles" | Erases the distinction you just drew |
| "any 2+ of these keywords = strong match" (with 40 keywords) | Nearly every listing qualifies |
| Seven archetypes each ending "STRONG MATCH" | A seven-way tie at the top |
| "when in doubt, ACCEPT" | Right intent, reads as "score it up" |

Keep the false-positive preference — it is usually correct — but state what it
means: *include it at its honest rank*, not *score it as strong*. A marginal
listing scored high costs you a real one, because only the top of the ranked
pool gets reviewed.

## 3. Use an ordered ladder, and write rank-down signals

Replace "how acceptable is this?" with "where does this rank?". An ordered
ladder where every role family lands in exactly one band:

```
Band A — <best-fit archetype, most specific>
Band B — <second>
...
Band G — everything else that survives the hard rejects
```

Then tie-breakers *within* a band. The **rank-down** list matters as much as
the rank-up list and is almost always missing:

> Rank DOWN (not reject): title fits but the described work is implementation
> under someone else's design · large org, one narrow slice · heavy on-call or
> platform maintenance · vague AI language with no concrete system described.

## 4. If a rule needs an exception, it is not a reject rule

Split "what I don't want" in two:

- **Literal rejects** — decidable from the listing text with no judgement.
  Named stacks (Kubernetes, Go, Terraform), named products (Salesforce, SAP),
  interview markers (LeetCode, "six rounds"), credential gates (JD required,
  MD required), geography, seniority ("intern", "new grad").
- **Rank down, do not reject** — anything needing a judgement call:
  "thin-wrapper AI", "primarily backend", "purely administrative",
  "pure governance".

A rule written as *reject-unless-one-of-six-exceptions* will be applied
inconsistently, and every misfire is a **false negative** — a good listing
dropped before you ever see it, silently. That is the most expensive failure
mode in the pipeline.

The tradeoff to make deliberately: moving judgement calls out of the reject
list means more listings survive into your queue. It does *not* cost more
tokens (everything reaches Stage 5 either way) — it costs queue depth and
review attention. If you prefer false positives, that is the right trade.

## 5. Keep keyword tiers small, and say they don't override the ladder

Tiered keywords are a good idea with two failure modes.

**Volume defeats the tier.** Forty keywords in Tier 1 with an "any 2+" rule
means Tier 1 no longer means "strongest signal", it means "AI-adjacent". Keep
Tier 1 small enough that matching it is informative.

**Keyword presence is not fit.** Listings are written by recruiters spraying
fashionable vocabulary. Say so directly:

> Keyword count is not fit. A listing that sprays Tier 1 vocabulary but
> describes narrow implementation work ranks below one using plainer language
> that clearly hands you system design or evaluation ownership.

## 6. Check for internal contradictions

Contradictions hurt an LLM judge more than a human reader: a human resolves
them from context, a model may follow whichever it saw last, inconsistently
across listings in the same batch. That shows up as ranking noise.

Real examples: "governance" appearing as a top-tier positive *and* in a reject
list; target titles including "Staff ML Engineer" while a reject rule covers
primarily-coding roles; "open to X **or other roles**" (pure noise to a
scorer).

Grep each major term and check it isn't doing double duty in opposite
directions.

## 7. Keep the profile in sync with your résumé

**`profile.md` is not only scoring context — it feeds résumé synthesis.** A
stale claim does not sit there being quietly wrong; `src/tailor.py` can
regenerate it into a tailored résumé and you can send it to an employer.

When you update your résumé, re-check `profile.md` in the same sitting:

- **Numbers** — publication counts, years, dollar figures, metrics — must
  match exactly.
- **Project names** must match. Both the judge and the résumé synthesizer use
  them.
- **Removed projects** must be purged from `profile.md` too, *including
  anywhere they are cited as evidence for a capability*.
- **Claims you softened or dropped** (certifications in progress, active
  pursuits) must be softened here too.
- **New competencies must reach the targeting sections** — target titles,
  keywords, the ladder — not just the biography. Adding a strength to "Who I
  am" does not change what the pipeline surfaces. This is the costly one: it
  silently under-surfaces the roles your newest work qualifies you for.

---

## Authoring checklist

1. Does the profile tell the judge to **discriminate**, or to approve? Search
   for "strong match", "equally", "score at the top" — each is a candidate
   defect.
2. Is there an **ordered ladder** where every role family lands in exactly one
   band?
3. Are there **rank-down** signals, not only rank-up?
4. Can every reject rule be applied **without an exception clause**? If not,
   move it to ranking.
5. Is Tier 1 **small enough that matching it means something**?
6. Does any term appear as both a positive and a negative?
7. Do all **numbers, project names, and credentials match the résumé** exactly?
8. Has every **recently added competency** reached the targeting sections?
9. Has every **removed project** been purged from evidence citations?
10. Does the **ladder fit inside the storable score range**? Anything a rank
    instruction can push below `CONFIDENCE_THRESHOLD` is deleted, not
    deprioritized.

## Settings that surprise people

Parsed from the `## Pipeline Settings` table (see
`src/profile_loader.py`). Two worth knowing:

- **`max_listings_per_run`** bounds Track B (email) only. Track A's volume is
  set by `results_wanted` in `search_config.yaml`.
- **`max_listing_age_days`** bounds the Slack digest. The CLI review queue has
  its own bound, `REVIEW_MAX_AGE_DAYS` (default 30). Set both, or listings can
  be visible on one surface and hidden on the other.
