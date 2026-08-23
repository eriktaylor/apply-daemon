---
name: apply-daemon
description: Drive the apply-daemon job-search pipeline — fetch fresh listings, then review and triage them. Use when the user wants to see what jobs came in, get new listings, review or triage matches, decide on listings (save/pass), tailor a resume for one, dig into why something scored the way it did, or asks "what's new" / "anything good today?" / "show me the top matches" in a job-search context.
---

# Apply Daemon — review surface

Walk the user through job listings the pipeline has already collected and
scored. The loop is: **show three → they pick → repeat.**

All work goes through `.venv/bin/python -m src.cli`, run from the repo root.
Every verb takes `--json`; parse that, never the prose output.

**Use `.venv/bin/python`, not `python`.** There is no `python` on PATH in a
default shell here, and `source .venv/bin/activate` does not persist between
tool calls. The explicit interpreter path always works and needs no setup.

Reviewing is free. **`refresh` and `enrich` are the two verbs that spend
metered money on their own** (any verb given `--via api` also does) — check
`status` first and say what it will cost.

## The loop

```
.venv/bin/python -m src.cli status --json            # worth running? can it afford to?
.venv/bin/python -m src.cli refresh --json          # get fresh listings (spends money)
.venv/bin/python -m src.cli enrich --json            # enrich already-stored rows (spends, ~10x cheaper than refresh)
.venv/bin/python -m src.cli next --top 3 --json      # a page of NEW candidates
.venv/bin/python -m src.cli next --seen --json       # the backlog: shown, still undecided
.venv/bin/python -m src.cli saved --json             # what they kept
.venv/bin/python -m src.cli sweep --json             # apply Slack reactions (free)
.venv/bin/python -m src.cli deep-dive <id> --json    # why it scored that way + research
.venv/bin/python -m src.cli save <id> --json         # they want it
.venv/bin/python -m src.cli pass <id> --json         # they don't
.venv/bin/python -m src.cli pass --all --json        # none of these three
.venv/bin/python -m src.cli show <id> --json         # detail without the dossier
.venv/bin/python -m src.cli tailor <id> --json       # tailor a resume (see below)
```

**Default motion — `sweep`, then `status`, then decide.** For "anything good
today?" / "what's new?", first run `sweep --json` (their phone reactions land
before you show them anything), then `status --json`. Both are free; sweep
takes a second or two of Slack I/O, status is instant. Status answers the
only question that matters: is there already fresh work?

- **`queue.ready > 0`** → run `next --top 3`. Present those. **Do not
  refresh.** A refresh takes 2–4 minutes and spends money to ingest listings
  that will not be reviewable until the *next* run enriches them, so
  refreshing on a stocked queue makes the user wait and pay for nothing they
  can act on now.
- **`queue.ready == 0`** → something has to run. With
  `queue.awaiting_enrichment > 0` and slots left, that is `enrich`; otherwise
  `refresh`. Say what it will cost and roughly how long, then run it — both
  chain into a page.
- **`queue.enrichment_remaining == 0`** → say so before refreshing. Autopilot
  is what produces reviewable cards and Slack posts; with its daily cap spent
  a refresh still ingests and still bills, but adds nothing to either surface
  until tomorrow. Offer `next`/`next --seen` instead.
- **`budget.can_run: false`** → report `reason` verbatim and fall back to
  `next`.

Prefer the cheap, instant answer. The user asked what is good today, not for
the pipeline to run.

## Reading the output

**`status`** returns `{verb, queue, budget}`.

- **`queue.ready` is the number that decides your next call** — listings that
  are fresh, enriched, and never shown. That is what `next` can hand back
  right now. `queue.fresh` is broader (includes rows autopilot has not
  enriched, which `next` will not show by default), `queue.reviewable` is
  broader still (includes stale). Ready 0 with a large stale count means
  "refresh"; ready 0 with a large `awaiting_enrichment` means the cap, not
  the queue, is the constraint.
- `queue.enriched_today` / `enrichment_cap` / `enrichment_remaining` are
  autopilot's daily budget. Remaining 0 means no new cards will appear on
  *either* surface — chat or Slack — until tomorrow, however many listings a
  refresh ingests.
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

**Show `effective_verdict` / `effective_confidence`, not `verdict` /
`confidence`.** A card carries two scores: Stage 5's (`verdict`,
`confidence`) and autopilot's re-score after it read the research dossier
(`post_research_verdict`, `post_research_confidence`). `effective_*` is
whichever one the card says to present, and `confidence_source` names it —
`"post_research"` or `"stage5"`. Say which, in the same breath as the number:
"MAYBE 58% (post-research)" and "YES 95% (stage 5)" mean different things,
and the feed is ordered by the effective one. Don't compute this yourself;
the card has already decided.

### How to present a listing

**Always include the `url`, as a clickable link, on every listing you show.**
This is not optional formatting. The user's next action is almost always
"open it and read the posting" — a card without its link is a dead end that
forces them back to Slack or a search engine to act on what you just showed
them. Slack cards have always carried the link; a chat summary that drops it
is strictly worse than the surface it replaces.

Also always include the `id`, so the user can name a listing for `deep-dive`,
`save`, `pass`, or `tailor` without you re-querying.

A good rendering is compact and complete:

```
1. Senior AI Engineer — Talkspace   ·  YES 95% (stage 5)  ·  Remote  ·  2d old
   Leading RL strategy + multi-agent architecture for a behavioral-health
   AI product. Skills 3/4 — has RL, multi-agent, healthcare AI; missing
   control theory.
   https://www.linkedin.com/jobs/view/4448240991
   id: 8f2c1a04
   (research cached — deep-dive is free)
```

Never summarize a page into prose that omits links and ids. The point of this
surface is speed *to a decision*, and a decision needs the posting.

- `tier` is `auto` / `auto_queued` / `triaged`. **`auto` means the research
  is already cached, so a deep-dive costs nothing** — say so when offering.
- `distance` (`Remote`/`Local`/`Commute`/`Far`) and `age_days` explain the
  ordering: within a quality band, nearer listings rank first. Mention the
  distance when presenting — the user asked for location-aware results.
- `hidden_stale` is how many listings were suppressed as older than
  `max_age_days`. Report it when nonzero. If the user wants them anyway,
  `next --max-age 0`.
- `count: 0` with `awaiting_enrichment > 0` means fresh listings exist but
  autopilot hasn't enriched them. This is the common empty page; offer
  `enrich` (below) rather than saying "nothing to review".
- `count: 0` with `hidden_stale > 0` means the queue is stale, not empty —
  say a refresh would bring new listings rather than "nothing to review".

**A thin page with `enrichment_remaining > 0` and `budget_can_run: true` is an
un-topped-up queue, not an empty one — offer `enrich`, not `refresh`.**
At `enrichment_remaining: 0` `enrich` refuses with
`error: "enrichment_capped"` rather than running — say the cap is spent and
that it resets at 00:00 UTC; do not reach for `--force`, which does not
override it.
`refresh` is for *new* listings; `enrich` converts the backlog that is already
stored. It spends metered money, so say so, but roughly a tenth of a refresh
and in a fraction of the time. Its envelope is refresh's with a single `stage`
instead of `stages`, and it chains into a `page` the same way. With
`enrichment_remaining: 0` neither verb adds cards today; with
`budget_can_run: false` neither may run at all.

**The feed never repeats itself.** A listing shown once leaves `next`
permanently — delivery already happened, and re-showing it is staleness, not
a reminder. Two consequences:

- `backlog` counts listings shown earlier that the user never decided on.
  **Mention it when nonzero** — that is the only thing keeping those rows
  visible. One clause is enough: "4 from earlier are still undecided."
- `next --seen` returns exactly those, and does not re-stamp them. Reach for
  it when the user says "what did I skip?", "show me those again", or asks
  about something they remember seeing. `saved` is the other half — listings
  they saved or tailored.

Do not treat an empty feed as an empty queue while `backlog` is nonzero.

**`sweep`** applies the reactions the user left in Slack — 👍 save, 👎 pass —
and returns any ✏️ ids in `pending_tailors`. It is free and fast.

- **Run it at the start of a session**, before `status`. The user triages from
  their phone; those decisions belong in the queue before you show them
  anything, or you will present listings they already passed on.
- `pending_tailors` are listings they marked ✏️ but that have **not** been
  tailored yet. Deliberate: Slack's own sweeper would spend ~$0.11 each
  through OpenRouter, while `cli tailor <id>` costs nothing because you answer
  the prompt. Offer to run them, and say the tailoring is free.
- Never tell the user to react ✏️ in Slack "and let the sweeper handle it" —
  that is the expensive path. From here, `cli tailor <id>` is strictly better
  and produces identical artifacts.

**`refresh`** runs the pipeline and returns the first page. It, `enrich`, and
any verb given `--via api`, are the only ways to spend metered money.

- Check `status` first — see "Default motion" above. Refreshing a stocked
  queue costs minutes and money for listings that are not yet reviewable.
- `--dry-run` shows the stages and the budget verdict without spending; use it
  when the user asks "what would that cost?".
- `ok: false` with `error: "budget_blocked"` means a cap or the cooldown
  refused it — the one `ok: false` where nothing ran at all. Report `reason` verbatim, then **fall back to `next`** — the
  user asked what's good, and the existing queue can still answer that. Say
  when the cooldown lifts. **Do not pass `--force`** unless the user
  explicitly asks — it exists for them, not for you.
- `spent_usd_this_run` is what the run actually cost. Report it.
- `page` is already the first page — render it (with urls and ids). Do not
  call `next` afterwards; that would page *past* what you just showed.
- **A refresh does not guarantee new cards.** Ingestion and enrichment are
  separate: a run can score 80 listings while autopilot enriches none, if its
  daily cap was already spent. If the page looks unchanged, check
  `status.enrichment_remaining` and say so plainly rather than implying the
  run failed.

**A stage can fail without the run failing.** `ok` means every stage
succeeded; it is not the same question as "did anything come in?".

- **`partial: true` is a run that worked with a hole in it.** One stage broke
  and every later stage still ran, so `page` holds real listings. Render the
  page and name the break in one clause — "3 came in; the Slack digest
  failed" — never "the refresh failed". `failed_stages` lists what broke;
  `failed_stage` is the first of them.
- **`skipped_stages` non-empty means the chain was abandoned** after two
  stages failed in a row, which points at a credential or a provider rather
  than a bad listing. Say what did not run — if `autopilot` is in that list,
  nothing new was enriched today — report `failed_stages`, and still show
  `page`. The queue can answer the user's question even when the run couldn't.
- A stage with `status: "detached"` has **no result to report**. Do not call
  it failed, and do not call it done.

**`refresh` returns before Slack posting finishes.** The two digest stages are
launched in the background (they were 16% of a measured run) and this verb
does not wait for them. Their cards land a minute or two later, so don't tell
the user Slack is up to date — the CLI page you just rendered is.

**`deep-dive`** returns `{verb, ok, listing, research, post_research}`.

- `post_research` is the *large* model's verdict after reading the research
  dossier, read straight from the dossier folder — so it is present here even
  for a listing enriched before the score was written to the row. It
  regularly disagrees with the Stage 5 `confidence` on the card — **that
  disagreement is the most useful thing on the screen.** Lead with it.
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
.venv/bin/python -m src.cli tailor <id>              # 1. prints the prompt
# ... you answer it, producing JSON ...
.venv/bin/python -m src.cli tailor <id> --apply -    # 2. pipe your JSON back in
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

Four more assets use the identical two-step handshake — emit, answer as
JSON, `--apply` it back — and are free the same way:

```bash
.venv/bin/python -m src.cli polish <id>          # final document; needs a prior tailor
.venv/bin/python -m src.cli cover-letter <id>
.venv/bin/python -m src.cli interview-prep <id>
.venv/bin/python -m src.cli answers <id> --questions "Why this company? ..."
```

Each accepts `--apply` and `--via api` exactly as `tailor` does, so read the
tailor section above and substitute the verb. `polish` integrates a previous
tailor's edits, so it errors with `unavailable` if none has run — tailor
first, then polish.

Prefer these over the Slack equivalents (`!polish`, `!coverletter`): the
reaction path is unattended, so it spends metered money for the same output.
`python -m src.batch_process` also spends — it exists for tailoring
everything saved at once, headless.

Slack thread commands (`!applied`, `!trend`, …) still work but are frozen —
see `docs/CHATOPS.md`. Don't build new workflows on them.
