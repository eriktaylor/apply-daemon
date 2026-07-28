---
name: apply-daemon
description: Review and triage job listings that the apply-daemon pipeline has already scraped, scored, and researched. Use when the user wants to see what jobs came in, review or triage matches, decide on listings (save/pass), dig into why something scored the way it did, or asks "what's new" / "show me the top matches" in a job-search context. Not for running the ingestion pipeline itself.
---

# Apply Daemon — review surface

Walk the user through job listings the pipeline has already collected and
scored. The loop is: **show three → they pick → repeat.**

All work goes through `python -m src.cli`. Every verb takes `--json`; parse
that, never the prose output. Run from the repo root.

## The loop

```
python -m src.cli next --top 3 --json      # a page of candidates
python -m src.cli deep-dive <id> --json    # why it scored that way + research
python -m src.cli save <id> --json         # they want it
python -m src.cli pass <id> --json         # they don't
python -m src.cli pass --all --json        # none of these three
python -m src.cli show <id> --json         # detail without the dossier
python -m src.cli tailor <id> --json       # tailor a resume (see below)
```

Start with `next`. Present the three listings compactly — title, company,
verdict + confidence, location, and whether a deep-dive is free. Then ask
what they want to do. Running `next` again pages forward; nothing is
consumed by being shown.

## Reading the output

**`next`** returns `{verb, count, listings[]}`. Each card has `id`, `title`,
`company`, `location`, `salary`, `verdict`, `confidence`, `status`, `tier`,
`research_cached`, `url`, `date_ingested`.

- `tier` is `auto` / `auto_queued` / `triaged`. **`auto` means the research
  is already cached, so a deep-dive costs nothing** — say so when offering.
- `count: 0` means the queue is empty, not an error.

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
