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

## Beyond triage

Tailoring a resume for a listing is not yet a CLI verb. If the user wants
that, tell them to react ✏️ on the listing's Slack card, or run
`python -m src.batch_process` for everything saved.

Slack thread commands (`!applied`, `!trend`, …) still work but are frozen —
see `docs/CHATOPS.md`. Don't build new workflows on them.
