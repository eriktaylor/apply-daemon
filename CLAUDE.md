# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (Python 3.11+ required)
uv sync && source .venv/bin/activate

# Lint + tests (CI runs exactly these two); single test: pytest tests/test_triage.py::test_name -q
ruff check .
pytest tests/ -q

# Full daily batch (thin wrapper over `src.cli refresh`, which owns the stage
# sequence and gates on the spend ceiling; args are forwarded)
./script.sh [--dry-run|--top-n N|--force]

# Daily entry points — full list (incl. one-time/smoke tools) in README.md#h-run-the-pipeline
python -m src.jobspy_ingest     # Track A: proactive JobSpy scrape
python -m src.pipeline          # Track B: email ingestion
python -m src.digest            # Post Slack digest cards
python -m src.sweeper           # Process Slack reactions + ChatOps commands
python -m src.cli status        # CLI review surface (also: refresh/enrich/next/saved/sweep/show/deep-dive/save/pass/tailor)
python -m src.process_queue     # Autopilot Speculative Agent (no-op unless AUTOPILOT_ENABLED=true)
python -m src.report --days 7   # Funnel metrics (--models, --spend)
```

## Architecture

Two ingestion tracks converge on a shared LLM scoring stage and a single SQLite store (`apply_daemon.db`), which then fans back out to two review surfaces. All metered LLM calls route through **OpenRouter**; in-session (subscription-billed) work is the exception and is exempt from the spend ceiling but visible in `status`.

**The pipeline diagrams live in [README.md](README.md#how-it-works)** — "Ingestion & scoring" and "Review & apply". They are not repeated here. The module→responsibility map is generated, not maintained — every `src/*.py` opens with a docstring whose first line is its responsibility: `grep -m1 '"""' src/*.py | sed 's/"""//'`.

Load-bearing behaviors a change can easily break, grouped by what they guard:

**Scoring**
- **Dedup runs *before* Stage 5** — already-known listings are skipped without spending tokens. The Smart Upsert afterwards handles races between tracks.
- **Three independent OpenRouter model slots** (`OPENROUTER_STAGE1_MODEL`, `OPENROUTER_MODEL` for Stage 5, `OPENROUTER_TAILOR_MODEL`, plus optional `OPENROUTER_TREND_MODEL`) let cost/quality be tuned per stage. See [docs/MODELS.md](docs/MODELS.md).
- **Stage 5 may batch** (`STAGE5_LISTWISE_BATCH`): `triage.prescore_batch`
  scores N listings per call and caches verdicts. Coverage is stochastic and
  logged; anything a batch omits — or a whole batch rejected on its anchor —
  **falls back to pointwise**. Never assume a listing was scored by one path.
- **`profile.md` is scoring context *and* résumé source material.** `triage`
  reads it to rank listings; `tailor` reads it to write documents the user
  sends to employers. A stale claim there is not inert — it can be
  regenerated into a tailored résumé. Authoring guidance:
  [docs/PROFILE.md](docs/PROFILE.md).
- **`CONFIDENCE_THRESHOLD` deletes; `NOISE_FLOOR_PCT` only declines to spend**
  — they default to the same value, which silently converts "rank this low"
  into "discard this" for any profile that ranks across a ladder. Mechanism
  and defaults: [docs/MODELS.md](docs/MODELS.md#confidence-threshold).
- **A rule that needs an exception is not a gate rule** — the profile-authoring
  version of this lesson is in [docs/PROFILE.md](docs/PROFILE.md); it
  generalizes to prompts too: push conditional judgement to the stage that
  has the context for it.

**Review feed**
- **`presented_at` is a delivery ledger, and the feed retires what it shows.** Re-showing was the defect: confidence is stable, so re-ranking one pool returns the same winners forever. Retirement is only safe because `next --seen` and `status.backlog` keep those rows reachable — see `db.get_review_queue`'s docstring, the only copy.
- **The feed ranks by the post-research re-score; both scores are kept.**
  Autopilot writes its re-score to `post_research_verdict` /
  `post_research_confidence` (`db.set_post_research_score`), *never* over
  Stage 5's columns — `deep-dive` reports the disagreement, so both have to
  survive. Every review query prefers the re-score
  (`db.EFFECTIVE_CONFIDENCE_SQL`) and every card labels which one it is
  showing (`listing_card.build_card`'s `confidence_source`). **The eval gold
  standard reads that JSON, not the DB** (`eval.listwise_compare.load_gold`)
  — keep it that way, or a backfill silently re-baselines every eval number.

**Spend & billing**
- **Autopilot** (`process_queue.py`) is a no-op unless `AUTOPILOT_ENABLED=true`. It pre-caches Deep Research so a CLI deep-dive costs nothing.
- **The re-score prefers the session route.** `AUTOPILOT_RESCORE_VIA=session` shells out through `src/claude_cli.py` (subscription-billed, never written to `logs/model_usage.log` — that file drives the spend ceiling). Falls back to OpenRouter on any failure. Stage 5 deliberately stays metered: per-call startup overhead is decisive against 100+ small calls. Measured overhead per invocation: [docs/MODELS.md](docs/MODELS.md#per-call-overhead).
- **A session-route call is a pure completion, and `claude_cli.run` is where that is enforced** — `--tools ""` (no tools, so no second turn), `--no-session-persistence`, and a neutral `cwd` so the judge does not inherit this repo's CLAUDE.md. Removing any of the three multiplies the tokens every call spends, and nothing else in the system would report it; `run` warns when the envelope's `num_turns` is not 1.

**Run orchestration**
- **`refresh` contains a stage failure instead of cancelling the chain, and
  returns before Slack does.** No stage reads another's exit code — all five
  communicate only through SQLite — so one failure says nothing about the
  next; *two consecutive* failures trip a circuit breaker and abandon the
  rest (`ok`/`partial`/`failed_stages` semantics: see the skill). Both
  `digest` stages launch detached, so autopilot may post Slack cards while a
  digest is running — `digest._already_delivered` guards that, and
  detaching turns off when `AUTOPILOT_POST_STAGE_5` puts both on the same
  rows. `--wait` restores the fully in-line chain for cron/CI.

### Configuration split

- **`.env`** — secrets + runtime knobs (model slots, `CONFIDENCE_THRESHOLD`, `GENERATE_ASSETS`, `AUTOPILOT_ENABLED`, `AUTOPILOT_POST_STAGE_5`, `MISMATCH_GATE_MODE`, `EXPIRED_PROBE_ENABLED`, Slack/Gmail/IPRoyal creds).
- **`my_profile/profile.md`** — candidate profile + Pipeline Settings table (`max_listings_per_run`, `dedup_window_days`, `home_location`, `max_listing_age_days`, etc.). Gitignored.
- **`my_profile/search_config.yaml`** — Track A only: `site_tiers` (friendly/ok/hostile) × `searches`. Runs N searches × M active tiers per cycle.
- **`my_profile_example/`** is the synthetic template; `my_profile/` is the user's gitignored copy.

## Security ground rules (from SECURITY.md)

- Never commit `.env`, `*.db`, `my_profile/`, or any `my_profile_*/` variant other than `my_profile_example/`.
- Test fixtures must be synthetic — no real listings, real emails, or real credentials.
- `plans/` is public — record aggregates, never rows. Raw output on real data goes in `data/reports/` (gitignored); the plan keeps a pointer. Full rule: SECURITY.md; enforced by `tests/test_plans_hygiene.py`.
- Logging must emit listing IDs + decisions only — **never raw email content, LLM prompts/responses, or credentials.**
- Don't weaken `.gitignore`, disable TLS verification, or add raw-content logging.

## Agent-facing behavior

The CLI (`src/cli.py`) is a machine interface primarily driven by the bundled skill in `.claude/skills/apply-daemon/` (per-verb guidance lives there); this section covers only what a coding agent working *on* the repo needs.

**`refresh` and `enrich` both chain into a page in the same call** (`page` in
their JSON) — a listing-producing verb should never require a follow-up call
to show its results. *When* to call `refresh` versus `enrich`/`sweep`/`next`
is runtime routing policy, owned by the skill, not this file.

**A new on-demand asset is a registry entry, never a new code path.**
`polish`, `cover-letter`, `interview-prep`, and `answers` share `tailor`'s
emit/apply handshake via `src/tailor.py`'s `ASSET_SPECS` registry plus a row
in `cli._ASSET_VERBS`. Enrichment is pre-cached by autopilot and tailoring
runs in-session — keep it that way: a read verb that makes a network call
breaks the conversational loop's latency and its cost story at once.

**`auto_queued` is backend state, not review material** — raw Stage 5 output
with no research and no large-model re-score. `next` shows enriched rows only
when `AUTOPILOT_POST_STAGE_5=false`; `--all-tiers` is the debugging escape.

**Card content is a contract, not formatting.** `src/listing_card.py` owns
what a review card contains; Slack and the CLI choose presentation only.
Derive anything derivable (skills %, distance label, freshness) rather than
asking the model — cheaper and unhallucinatable — and let missing data become
a stated absence, never an exception or a silent omission.
`tests/test_listing_card.py` enforces all of it.

## Anti-drift

### One fact, one home

Every idea, feature, number, or result is written **once**, in the surface
that owns it. Everywhere else points at it.

| Surface | Audience | Owns |
|---|---|---|
| `README.md` | humans evaluating or setting up the project | what it does, how to run it, the ASCII architecture diagrams |
| `CLAUDE.md` | coding agents | invariants, conventions, the trap that looks like a bug — never what the code states plainly |
| `docs/*.md` | reference | the deep version of one topic (profile authoring, models, chatops, cli, proxy, audit, eval) |
| `.claude/skills/` | the runtime agent | when to call which verb, how to read its output |
| `plans/*.md` | planning (gitignored) | what shipped, what's next, and why |

Duplicated prose or code does not stay duplicated — it *diverges*, and
nothing signals which copy is right (CLAUDE.md once carried its own copy of
the pipeline diagram and silently went stale). When editing or extracting:

- **A pointer must not restate.** "See `docs/MODELS.md` for the confidence
  bands" is a pointer. "See `docs/MODELS.md` — the default is 0.5" is a
  second copy of the fact, and it will drift.
- **Grep the behavior, not the name, before writing a helper.** Names don't
  match across authors — match what code *does* (`job_id[:8]`, `split("|")`,
  the column being written); `_output_folder` would never have found the
  existing `_find_existing_output`.
- **One constant, one definition.** A comment reading "matches
  `other._THING`" is drift documented as drift; import it instead.
- **Two callers means extract, not copy.** Move shared logic somewhere both
  can import — even a new small module — rather than reimplementing it;
  that's why `human_labels.py`, `model_usage.py`, and `ranking.py` exist.
- **Adapters, never parallel implementations.** Slack, the CLI, and
  `script.sh` are entry points over shared logic; an entry point that
  reimplements a transition is a defect however well it works.
- **Register what you extract.** A source-level test asserting one concept →
  one implementation site is the only layer that survives forgetting — see
  `TestMeteringCoverage` in `tests/test_model_usage.py`. Add the entry when
  you extract, while the decision is fresh.

### Audit checklist

When auditing your own or prior work, rely on explicit steps rather than instincts:

1. **Audit claims & logic** — Re-check assertions against the code, including ones made earlier in the same session. Actively inspect for logical errors, hidden assumptions, and structural bugs.
2. **Duplication** — Identify what this change added that already existed. Grep the underlying behavior across the codebase, not just the function names.
3. **Coherence** — Render every affected command using real, live data. Step back and look at the final output: do all user-facing surfaces tell one unified story?
4. **Context drift** — Grep every document, comment, or system prompt mentioning the changed behavior. Fix the structural owner, eliminate drifting focus, and verify all pointers still read true.

## Conventions

- LLM prompt templates (`triage.py`, `tailor.py`) are one prose sentence per line so the wire-format survives — do not reflow them. The E501/E402 exemptions and their rationale live in `pyproject.toml`'s `per-file-ignores`.
- Squash on merge; commit messages on `main` read like changelog entries.
- **Stage 5 or ranking model swaps must pass the gate** — `python -m
  eval.listwise_compare --gold --shuffle --model <slug>` — before any slug
  change; version numbers are not fitness. The gate's own metric is weak, so
  also check a ranking metric. `data/human_labels.jsonl` is the only
  non-proxy ground truth. Full caveat:
  [docs/MODELS.md](docs/MODELS.md#pending-model-slot-review).
- **Slack thread commands (`!applied`, `!triage`, `!trend`, …) are frozen** — they still work, but new post-triage functionality belongs in the CLI review surface, not `sweeper.py`. Slack *reactions* (👍 👎 ✏️ ❓) are unaffected. See `docs/CHATOPS.md`.
- **Every human decision must append to `data/human_labels.jsonl`** via `src/human_labels.py::append_human_label` (pass `surface=`). That ledger is the only input to the preference-pair extractor behind the ranking evals — a surface that skips it is invisible to that work, silently.
- **Metered spend must be auditable.** Any code path that calls OpenRouter routes its token count through `src/model_usage.py::log_model_usage` (model, stage, tokens — never prompt or response content). `logs/model_usage.log` is the audit trail and the basis for spend ceilings (`src/budget.py`); a spending path missing from it makes the budget unenforceable. `tests/test_model_usage.py::TestMeteringCoverage` greps every `chat.completions.create` call site in `src/` and fails the suite if one doesn't log its usage. In-session (subscription-billed) work is exempt: it has no metered cost to record.
