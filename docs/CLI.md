# CLI Reference

This is the vocabulary reference for the *live* review surface,
`python -m src.cli` — the successor to Slack thread-ChatOps (frozen; see
[`docs/CHATOPS.md`](CHATOPS.md)). For every verb it documents the flags, the
JSON envelope's top-level keys, whether it mutates anything, and its cost
tier.

**This page is not routing policy.** *When* to call `next` vs `refresh` vs
`enrich`, how to read `status` to decide, and how the tailor handshake plays
out in a session are [`.claude/skills/apply-daemon/SKILL.md`](../.claude/skills/apply-daemon/SKILL.md)'s
job — it's the runtime agent's judgment layer. This page is the dictionary
it's read against.

Every verb accepts `--json` (either side of the verb name) to emit the
envelope documented below; omit it for an equivalent human-readable
rendering. `--json` is left out of the flag lists below since it's universal.
The envelope shapes here are pinned by `tests/test_cli.py` — added keys are
compatible; renamed or removed keys are a breaking change to this page.

## Verbs at a glance

| Verb | Tier | Mutates |
|---|---|---|
| `next` | free read | yes — retires what it shows (see below) |
| `next --seen` | free read | no |
| `saved` | free read | no |
| `status` | free read | no |
| `show <id>` | free read | no |
| `deep-dive <id>` | free read | no |
| `sweep` | free write | yes — applies Slack reactions |
| `save <id>` / `save --all` | free write | yes |
| `pass <id>` / `pass --all` | free write | yes (terminal) |
| `tailor <id>` | session-billed | inert until `--apply` or `--via api` |
| `polish <id>` | session-billed | inert until `--apply` or `--via api` |
| `cover-letter <id>` | session-billed | inert until `--apply` or `--via api` |
| `interview-prep <id>` | session-billed | inert until `--apply` or `--via api` |
| `answers <id>` | session-billed | inert until `--apply` or `--via api` |
| `refresh` | metered | yes — runs the ingestion chain |
| `enrich` | metered | yes — runs autopilot alone |

## Reading a page

`next`, `refresh`, and `enrich` all hand back the same page shape — `next`'s
envelope *is* the page (merged with `verb`); `refresh` and `enrich` nest an
identical page under their `page` key. Top-level page keys:

`count`, `listings`, `max_age_days`, `seen`, `hidden_stale`,
`awaiting_enrichment`, `backlog`, `tiers`, `enrichment_remaining`,
`budget_can_run`

Each entry in `listings` is a card. Read `effective_verdict` /
`effective_confidence`, not the raw `verdict` / `confidence` — and read
`confidence_source` (`"stage5"` or `"post_research"`) to know which one
you're looking at. What the two scores mean and why both are kept is owned
by CLAUDE.md's "Review feed" bullets, under [Architecture](../CLAUDE.md#architecture) —
this page doesn't re-explain it. The full card field set (skills match,
distance, freshness, TL;DR, …) is owned by
[`src/listing_card.py`](../src/listing_card.py); every review surface
renders a subset of exactly that set, never its own.

## Free reads

Read-only: no LLM calls, no network, so the conversational loop stays
instant.

### `next`

**Flags:** `--top N` (default 3) · `--all-tiers` (include un-enriched Stage 5
rows) · `--max-age DAYS` (default 30; `0` disables) · `--seen`

**Mutates:** yes, unless `--seen` is given. `next` stamps `presented_at` on
the rows it returns and the feed retires them — a listing shown once will
not be shown again by a plain `next`. See `get_review_queue`'s docstring in
[`src/db.py`](../src/db.py) for the authoritative reasoning; it is not
repeated here. `next --seen` queries the backlog (shown earlier, still
undecided) instead, and does not re-stamp anything.

**Returns:** the page shape above, plus `verb`. Note there is no `ok` key.

### `saved`

**Flags:** `--top N` (default 10)

**Mutates:** no.

**Returns:** `{verb, ok, count, listings}` — listings you saved or already
tailored.

### `status`

**Flags:** none.

**Mutates:** no.

**Returns:** `{verb, queue, budget}`.

- `queue`: `reviewable`, `fresh`, `ready`, `backlog`, `awaiting_enrichment`,
  `enrichment_cap`, `enriched_today`, `enrichment_remaining`, `stale_hidden`,
  `max_age_days`, `by_tier`, `total_listings`, `last_ingest`,
  `last_ingest_age_hours`, `last_decision`.
- `budget`: `can_run`, `reason`, `spent_usd_today`, `spent_tokens_today`,
  `budget_usd`, `remaining_usd`, `minutes_since_run`.

### `show <id>`

**Flags:** `id` (positional, required).

**Mutates:** no.

**Returns:** `{verb, ok, listing}` on a hit — the card, plus `reason` (why
Stage 5 scored it that way). `{verb, ok: false, error: "not_found", id}` on a
miss.

### `deep-dive <id>`

**Flags:** `id` (positional, required).

**Mutates:** no. Free because autopilot pre-caches the research dossier
before this verb ever runs; a cache miss is reported, never filled in live.
`research.cached: false` means exactly that — no dossier exists yet, and
this verb will not generate one inline.

**Returns:** `{verb, ok, listing, research, post_research}` on a hit.
`research` is `{cached, folder, context}`. `post_research` is `null` when
autopilot hasn't re-scored this listing yet, otherwise the normalized
re-score — `verdict`, `confidence`, `confidence_delta`, `match_analysis`,
`matching_skills`, `missing_skills` — built by
`parse_post_research` in [`src/listing_card.py`](../src/listing_card.py).
`{verb, ok: false, error: "not_found", id}` on a miss.

## Free writes

No metered spend. `sweep` does Slack network I/O; the decision verbs are
local DB writes.

### `sweep`

**Flags:** `--limit N` (default 50, messages to scan).

**Mutates:** yes — applies Slack reactions (👍 save, 👎 pass) via the same
dispatch `python -m src.sweeper` uses. ✏️ reactions are not tailored here;
their ids come back for `tailor <id>` to handle instead.

**Returns:** `{verb, ok: true, passed, saved, skipped, pending_tailors}` on
success. `{verb, ok: false, error: "sweep_failed", detail}` if the sweep
itself raised.

### `save <id>` / `pass <id>` / `pass --all`

**Flags:** `id` (positional; omit only with `--all`) · `--all` (act on the
current page — `save` and `pass` are the only verbs `--all` applies to).

**Mutates:** yes — writes `pipeline_status` and appends a row to
`data/human_labels.jsonl` on every decision. See CLAUDE.md's
[Conventions](../CLAUDE.md#conventions) for the ledger invariant this
protects; not restated here. `pass` is terminal: `src/decisions.py` refuses
a later `save` out of `passed` or `expired` (mirrors the documented Slack
rule that 👎 is terminal — [`docs/CHATOPS.md`](CHATOPS.md#pass-and-expire)).

**Returns (`--all`):** `{verb, ok: true, ids, count, bulk: true}`.
**Returns (single id):** `{verb, ok: true, id, status, bulk: false}` on
success; `{verb, ok: false, error: "not_found", id}` for an unknown id;
`{verb, ok: false, error: "no_transition", id, status}` when the row was
already at the target status or the decision policy refused it.

## Session-billed

`tailor` and the four on-demand assets share one two-step handshake: the
verb emits a prompt, the session answers it, `--apply` writes the result.
The mechanics of that handshake — what the prompt contains, how to answer
it, what step 2 validates — are documented once, in SKILL.md's
["Tailoring a resume"](../.claude/skills/apply-daemon/SKILL.md#tailoring-a-resume)
and ["Beyond triage"](../.claude/skills/apply-daemon/SKILL.md#beyond-triage)
sections; this page covers only the CLI surface of that handshake.

Every verb in this tier accepts `--via api`, which routes it through
OpenRouter instead and makes it metered.

### `tailor <id>`

**Flags:** `id` · `--apply PATH` (`-` for stdin) · `--via {session,api}`
(default `session`).

**Mutates:** the default call (no `--apply`, no `--via api`) is inert — it
only emits a prompt, no status change, no ledger row. `--apply` or
`--via api` write the assets to `output/`, set `pipeline_status` to
`tailored`, and append to the ledger.

**Returns (emit):** `{verb, ok: true, id, route: "in_session",
stage: "prompt", prompt, research_cached, apply_with}`.
**Returns (`--apply`):** `{verb, ok: true, id, route: "in_session", folder,
status: "tailored"}`, or `{verb, ok: false, error: "invalid_response"|
"empty_input", id, detail?}`.
**Returns (`--via api`):** `{verb, ok: true, id, route: "api", folder,
status: "tailored"}`, or `{verb, ok: false, error: "api_failed", id,
detail}`.
`{verb, ok: false, error: "not_found", id}` on an unknown id in any mode.

### `polish <id>` / `cover-letter <id>` / `interview-prep <id>` / `answers <id>`

**Flags:** `id` · `--apply PATH` · `--via {session,api}` · `answers` only:
`--questions "..."`.

**Mutates:** same pattern as `tailor` — inert on emit, writes on `--apply` or
`--via api`.

**Returns:** the same shape family as `tailor`'s, with `verb` set to the
asset's wire name (`polish`, `cover-letter`, `interview-prep`, `answers`).
Two error codes are specific to this tier: `questions_required` (`answers`
called with no `--questions` and nothing being applied) and `unavailable`
(the asset's prerequisite isn't met — e.g. `polish` before any `tailor` has
run for that listing).

## Metered

The only two verbs that spend money on their own (besides any verb given
`--via api`). Both are gated by `budget.py`'s daily ceiling and run
cooldown, and both chain into a page so a listing-producing verb never needs
a follow-up call to show its results.

`refresh` is for *new* listings — it scrapes and scores. `enrich` is for
converting the backlog that is already stored — research and re-score only,
no scrape, no email — and costs roughly a tenth of a refresh in a fraction
of the time. Which one to reach for on a given queue state is SKILL.md's
call, not this page's.

### `refresh`

**Flags:** `--top-n N` (autopilot's enrichment budget for this run only) ·
`--force` (run despite a budget refusal) · `--dry-run` (report the plan and
budget verdict, run nothing) · `--no-next` (skip the trailing page) ·
`--wait` (run the Slack digest stages in-line instead of detaching them).

**Mutates:** yes — runs the ingestion chain
(`jobspy_ingest → digest → pipeline → digest → process_queue`) as
subprocesses, and records the run against the cooldown before any stage
executes.

**Returns (`--dry-run`):** `{verb, ok: true, dry_run: true, would_run,
allowed, reason, spent_usd_today, budget_usd}` — nothing runs.
**Returns (budget refused, no `--force`):** `{verb, ok: false,
error: "budget_blocked", reason, spent_usd_today, budget_usd}` — the one
`ok: false` where nothing ran at all.
**Returns (ran):** `{verb, ok, partial, stages, failed_stage, failed_stages,
skipped_stages, spent_usd_this_run, spent_usd_today, page}` — `page` is
`null` if `--no-next` was given. Each entry of `stages` is `{stage, module,
status, returncode, seconds, log}`, `status` one of `"ok"` / `"failed"` /
`"detached"`.

### `enrich`

**Flags:** `--top-n N` · `--force`. (No `--dry-run`, `--no-next`, or
`--wait` — it runs exactly one stage.)

**Refuses when the day's enrichment cap is spent**, before the cooldown is
recorded — a capped run would enrich nothing and still block the next genuine
one. `--force` does not override this (it overrides a budget judgement, not an
exhausted cap); `--top-n N` raises the cap for the run, and the check counts
against it. `refresh` has no such guard: it still scrapes and scores with the
cap spent.

**Mutates:** yes — runs `python -m src.process_queue` alone against rows
already stored, gated and recorded exactly as `refresh` gates and records.

**Returns (budget refused, no `--force`):** the same `budget_blocked` shape
as `refresh`, with `verb: "enrich"`.
**Returns (cap spent):** `{verb, ok: false, error: "enrichment_capped",
reason, enrichment_cap, enriched_today, enrichment_remaining,
budget_can_run}` — nothing ran, and the cooldown was not started.
**Returns (ran):** `{verb, ok, stage, spent_usd_this_run, spent_usd_today,
page}` — `stage` is a single record in the same shape as one of `refresh`'s
`stages[]` entries, and `page` always chains.

## Error codes

A quick lookup for the `error` values above; what to do about each is
SKILL.md's call, not this table's.

| Code | Seen from | Meaning |
|---|---|---|
| `not_found` | `show`, `deep-dive`, `tailor`, the asset verbs, `save`, `pass` | no listing with that id |
| `no_transition` | `save`, `pass` | already at the target status, or refused by `src/decisions.py` (terminal status, or a downgrade a save may not make) |
| `budget_blocked` | `refresh`, `enrich` | the spend ceiling or run cooldown refused; nothing ran |
| `enrichment_capped` | `enrich` | the day's enrichment cap is spent; nothing ran and no cooldown was started |
| `empty_input` | `tailor`, the asset verbs | `--apply -` read nothing from stdin |
| `invalid_response` | `tailor`, the asset verbs | the piped JSON didn't parse or validate; nothing was written |
| `unavailable` | the asset verbs | the asset's prerequisite isn't met yet |
| `questions_required` | `answers` | no `--questions` given and nothing being applied |
| `api_failed` | `tailor`, the asset verbs (`--via api`) | the OpenRouter call failed |
| `sweep_failed` | `sweep` | the Slack sweep raised; nothing was applied |
