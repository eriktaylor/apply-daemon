---
name: apply-daemon
description: Drive the apply-daemon job-search pipeline — fetch fresh listings, then review and triage them. Use when the user wants to see what jobs came in, get new listings, review or triage matches, decide on listings (save/pass), tailor a resume for one, dig into why something scored the way it did, or asks "what's new" / "anything good today?" / "show me the top matches" in a job-search context.
---

# Apply Daemon — review surface

Walk the user through job listings the pipeline has already collected and
scored. The loop is: **show three → they pick → repeat.**

All work goes through `python -m src.cli`. Every verb takes `--json`; parse
that, never the prose output. Run from the repo root.

Reviewing is free. **`refresh` is the one verb that spends metered money** —
check `status` first and say what it will cost.

## The loop

```
python -m src.cli status --json            # worth running? can it afford to?
python -m src.cli refresh --json          # get fresh listings (spends money)
python -m src.cli next --top 3 --json      # a page of candidates
python -m src.cli deep-dive <id> --json    # why it scored that way + research
python -m src.cli save <id> --json         # they want it
python -m src.cli pass <id> --json         # they don't
python -m src.cli pass --all --json        # none of these three
python -m src.cli show <id> --json         # detail without the dossier
python -m src.cli tailor <id> --json       # tailor a resume (see below)
```

Start with `next`; present the three listings compactly — title, company,
verdict + confidence, location/distance, age, and whether a deep-dive is
free — then ask what they want to do. Running `next` again pages forward;
nothing is consumed by being shown. Lead with `status` instead when the user
opens with a broad question ("anything new?", "what's the state of
things?") — it is free, and it says whether the queue already has fresh
work or a refresh is the right move.

## Reading the output

**`status`** returns `{verb, queue, budget}`.

- `queue.fresh` is the number that matters: undecided listings inside the
  freshness window. `queue.reviewable` is the total including stale;
  `queue.by_tier` splits it into `auto` / `auto_queued` / `triaged`.
  Fresh 0 with a large stale count means "refresh", not "all done".
- `queue.last_ingest_age_hours` is how stale the newest listings are.
- `budget.can_run` is whether a pipeline run is permitted right now, with
  `budget.reason` explaining it. Report the reason verbatim when it is
  `false` — "blocked by cooldown for another 40 minutes" is actionable;
  "couldn't run" is not.
- A deep queue plus recent ingest means the useful next step is `next`, not
  a refresh. Say so rather than defaulting to fetching more.

**`next`** returns `{verb, count, listings[]}`. Each card has `id`, `title`,
`company`, `location`, `salary`, `verdict`, `confidence`, `status`, `tier`,
`research_cached`, `url`, `date_ingested`.

- `tier` is `auto` / `auto_queued` / `triaged`. **`auto` means the research
  is already cached, so a deep-dive costs nothing** — say so when offering.
- `distance` (`Remote`/`Local`/`Commute`/`Far`) and `age_days` explain the
  ordering: within a quality band, nearer listings rank first. Mention the
  distance when presenting — the user asked for location-aware results.
- `hidden_stale` is how many listings were suppressed as older than
  `max_age_days`. Report it when nonzero. If the user wants them anyway,
  `next --max-age 0`.
- `count: 0` with `hidden_stale > 0` means the queue is stale, not empty —
  say a refresh would bring new listings rather than "nothing to review".

**`refresh`** runs the pipeline — **the only verb that spends metered money.**

- Check `status` first: if `queue.fresh` is healthy, reviewing beats refreshing.
- `--dry-run` shows the stages and the budget verdict without spending; use it
  when the user asks "what would that cost?".
- `ok: false` with `error: "budget_blocked"` means a cap or the cooldown
  refused it. Report `reason` verbatim. **Do not pass `--force`** unless the
  user explicitly asks — it exists for them, not for you.
- `spent_usd_this_run` is what the run actually cost. Report it.
- On success, follow with `next`.

**`deep-dive`** returns `{verb, ok, listing, research, post_research}`.

- `post_research` is the *large* model's verdict after reading the research
  dossier. It regularly disagrees with the Stage 5 `confidence` on the card —
  **that disagreement is the most useful thing on the screen.** Lead with it.
- `confidence_delta` is post-research minus Stage 5. It skews negative
  (typically around −20): the first pass is optimistic. A large negative
  delta means "looked better from the outside than it is."
- `post_research: null` means autopilot hasn't reached this listing yet.
- `research.cached: false` means no dossier exists. Say so and move on — do
  **not** offer to generate one inline; that is a slow, token-spending call.

**Decisions** return `{verb, ok, id, status, bulk}`. `ok: false` with
`error: "no_transition"` means it was already in that state, or you tried to
save something already passed (passing is terminal). Not a failure worth
retrying — just tell the user.

## Rules

- **Never write SQL or touch `apply_daemon.db` directly.** The CLI is the
  only writer. Ad-hoc *reads* for debugging are fine via a read-only URI.
- **Never invent a listing id.** Use ids returned by `next`/`show`.
- **Confirm before `pass --all`.** It acts on the whole current page and
  passing is terminal; a misread "pass" costs the user real opportunities.
- **Don't re-rank or filter the page yourself.** Ordering already reflects
  tier and confidence. Present what you're given, in the order given.
- Every decision is recorded as training data for the ranking model, so
  route decisions through the CLI rather than just discussing them.

## Tailoring a resume

Two steps, and **you** are the model in the middle — that is the point. The
work is billed to this session rather than metered per token, so prefer it.

```
python -m src.cli tailor <id>              # 1. prints the prompt
# ... you answer it, producing JSON ...
python -m src.cli tailor <id> --apply -    # 2. pipe your JSON back in
```

Step 1 emits a prompt containing the candidate profile, base resume, the
listing, and cached research. Answer it yourself and return **only** a JSON
object — `match_analysis` is required, plus whatever asset keys the prompt
asks for (`resume_bullet_edits`, `custom_cover_letter`, …). Step 2 validates
it, writes the `.docx` assets to `output/`, and marks the listing `tailored`.

Step 1 is free and changes nothing, so it's safe to run and then stop if the
user changes their mind. Only step 2 commits.

Research in the prompt comes from the cached dossier only
(`research_cached` in the JSON says which). If the prompt says none is
available, that is not an error — tailor from the job description, or use
`--via api` if the user explicitly wants live research (metered).

Use `--via api` **only** when the user explicitly asks to spend API credit,
or when no session can do the work (a cron or batch run). It routes through
OpenRouter's tailor model and costs real money. When in doubt, do it
yourself.

If step 2 rejects your JSON (`error: "invalid_response"`), fix the JSON and
retry — nothing was written.

## Beyond triage

Cover letters, interview prep, and `!polish` are still Slack-only: react ✏️
on the card, or run `python -m src.batch_process` for everything saved.

Slack thread commands (`!applied`, `!trend`, …) still work but are frozen —
see `docs/CHATOPS.md`. Don't build new workflows on them.
