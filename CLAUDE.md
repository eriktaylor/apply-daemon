# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (Python 3.11+ required)
uv sync && source .venv/bin/activate

# Lint + tests (CI runs exactly these two)
ruff check .
pytest tests/ -q

# Single test
pytest tests/test_triage.py::test_name -q

# Full daily batch (Track A → digest → Track B → digest → autopilot)
./script.sh

# Individual entry points
python -m src.jobspy_ingest     # Track A: proactive JobSpy scrape
python -m src.pipeline          # Track B: email ingestion
python -m src.digest            # Post Slack digest cards
python -m src.sweeper           # Process Slack reactions + ChatOps commands
python -m src.cli next --top 3  # CLI review surface (also: show/deep-dive/save/pass)
python -m src.batch_process     # Concurrent tailor for all saved listings
python -m src.process_queue     # Autopilot Speculative Agent (no-op unless AUTOPILOT_ENABLED=true)
python -m src.process_queue --backfill        # Promote existing YES/MAYBE into autopilot queue
python -m src.integration_test  # Config + reachability check (use --no-llm / --no-network to skip)
python -m src.report --days 7   # Funnel metrics (--models, --spend)
python -m src.geo_backfill      # One-time distance_bucket backfill (--dry-run first)
```

## Architecture

Two ingestion tracks converge on a shared LLM scoring stage and a single SQLite store (`apply_daemon.db`), which then fans back out to two review surfaces. All LLM calls route through **OpenRouter**, except in-session tailoring.

**The pipeline diagrams live in [README.md](README.md#how-it-works)** — "Ingestion & scoring" and "Review & apply". They are not repeated here; see the anti-drift principle below. What follows is the agent-facing map: which module owns what.

Load-bearing behaviors a change can easily break:

- **Dedup runs *before* Stage 5** — already-known listings are skipped without spending tokens. The Smart Upsert afterwards handles races between tracks.
- **Three independent OpenRouter model slots** (`OPENROUTER_STAGE1_MODEL`, `OPENROUTER_MODEL` for Stage 5, `OPENROUTER_TAILOR_MODEL`, plus optional `OPENROUTER_TREND_MODEL`) let cost/quality be tuned per stage. See [docs/MODELS.md](docs/MODELS.md).
- **Autopilot** (`process_queue.py`) is a no-op unless `AUTOPILOT_ENABLED=true`. It pre-caches Deep Research so a CLI deep-dive costs nothing.
- **`presented_at` is paging state, not a gate.** The reasoning, and why gating on it would strand a shown page, is in `db.get_review_queue`'s docstring — the only copy.

### Project structure

```
apply-daemon/
├── my_profile_example/          # Template — cp -r to my_profile/ (synthetic only; committed)
│   ├── profile.md
│   ├── base_resume.docx
│   ├── cover_letter.md
│   └── search_config.yaml       # JobSpy search config (Track A) — generic ML/AI engineer starter
├── my_profile/                  # User's data + customized search_config.yaml (GITIGNORED)
├── src/
│   ├── jobspy_ingest.py         # Track A — proactive JobSpy polling
│   ├── pipeline.py              # Track B — silent worker (fetch, triage, store)
│   ├── digest.py                # Slack digest (posts listings for reactions)
│   ├── sweeper.py               # Reaction sweeper + ChatOps parser. Priority: pass > tailor > save. Idempotent.
│   │                            # THREAD COMMANDS ARE FROZEN — see Conventions.
│   ├── human_labels.py          # Shared human-feedback ledger writer (data/human_labels.jsonl)
│   ├── cli.py                   # CLI review surface (next/show/deep-dive/save/pass). Local-only: no LLM, no network.
│   ├── tailor.py                # Cloud LLM escalation engine (multi-line prompts; E501 ignored)
│   ├── compile.py               # .docx generation from tailored bullets
│   ├── research.py              # Deep Research agent (semantic scraping; runs before every tailor)
│   ├── report.py                # CLI funnel report
│   ├── batch_process.py         # Concurrent OpenRouter tailor requests for every saved listing
│   ├── process_queue.py         # Autopilot Speculative Agent (no-op unless AUTOPILOT_ENABLED=true)
│   ├── email_fetcher.py         # IMAP connection + retrieval
│   ├── email_classifier.py      # Header-only regex classification (no LLM)
│   ├── text_extractor.py        # Generic HTML → text (no platform-specific parsers)
│   ├── triage.py                # Stage 5 LLM scoring (multi-line prompts; E501 ignored)
│   ├── mismatch_gate.py         # Autopilot: hybrid title↔body gate (substring → LLM fallback)
│   ├── expired_probe.py         # Autopilot: HTTP backstop for expired/dead listings
│   ├── audit_log.py             # Pipe-delimited audit log for silent drops (see docs/AUDIT.md)
│   ├── geo.py                   # Nominatim geocoding + LRU cache + haversine
│   ├── models.py                # JobListing dataclass
│   ├── profile_loader.py        # Loads profile.md (Pipeline Settings table drives runtime knobs)
│   ├── notifications.py         # Slack Block Kit posting + rate-limited retry
│   ├── proxy_manager.py         # IPRoyal sticky residential rotator
│   ├── proxy_test.py            # CLI smoke test for the IPRoyal stack
│   ├── integration_test.py      # Pre-flight reachability + config check
│   ├── file_utils.py            # Shared filesystem helpers
│   └── db.py                    # SQLite schema + data access (Smart Upsert, fuzzy dedup, autopilot queue)
├── eval/                        # Labeled-data eval harness
├── tests/                       # pytest suite (synthetic fixtures only)
├── docs/                        # CHATOPS.md, MODELS.md, PROXY.md, EVAL_GUIDE.md, PROJECT_BRIEFING.md, AUDIT.md
├── script.sh                    # Daily batch chain (jobspy_ingest → digest → pipeline → digest → process_queue)
├── pyproject.toml               # Direct dependencies + loose version constraints
├── requirements.lock            # Autogenerated full resolution (uv pip compile)
└── apply_daemon.db              # SQLite store (GITIGNORED)
```

### Configuration split

- **`.env`** — secrets + runtime knobs (model slots, `CONFIDENCE_THRESHOLD`, `GENERATE_ASSETS`, `AUTOPILOT_ENABLED`, `AUTOPILOT_POST_STAGE_5`, `MISMATCH_GATE_MODE`, `EXPIRED_PROBE_ENABLED`, Slack/Gmail/IPRoyal creds).
- **`my_profile/profile.md`** — candidate profile + Pipeline Settings table (`max_listings_per_run`, `dedup_window_days`, `home_location`, `max_listing_age_days`, etc.). Gitignored.
- **`my_profile/search_config.yaml`** — Track A only: `site_tiers` (friendly/ok/hostile) × `searches`. Runs N searches × M active tiers per cycle.
- **`my_profile_example/`** is the synthetic template; `my_profile/` is the user's gitignored copy.

## Security ground rules (from SECURITY.md)

- Never commit `.env`, `*.db`, `my_profile/`, or any `my_profile_*/` variant other than `my_profile_example/`.
- Test fixtures must be synthetic — no real listings, real emails, or real credentials.
- Logging must emit listing IDs + decisions only — **never raw email content, LLM prompts/responses, or credentials.**
- Don't weaken `.gitignore`, disable TLS verification, or add raw-content logging.

## Design principles

### One fact, one home

Every idea, feature, number, or result is written **once**, in the surface
that owns it. Everywhere else points at it.

| Surface | Audience | Owns |
|---|---|---|
| `README.md` | humans evaluating or setting up the project | what it does, how to run it, the ASCII architecture diagrams |
| `CLAUDE.md` | coding agents | repo behavior, invariants, conventions, module→responsibility map |
| `docs/*.md` | reference | the deep version of one topic (models, chatops, proxy, audit, eval) |
| `.claude/skills/` | the runtime agent | when to call which verb, how to read its output |
| `plans/*.md` | planning (gitignored) | what shipped, what's next, and why |

### Anti-drift

Duplicated prose does not stay duplicated — it *diverges*, and then two
documents disagree with no signal about which is right. This has already
happened here: CLAUDE.md carried its own copy of the pipeline diagram, and
it silently went stale, still ending at "reactions handled by sweeper.py"
long after the CLI review surface shipped.

When editing:

- **Before explaining something, grep for it.** If an explanation exists,
  link to it instead of writing a second one.
- **A pointer must not restate.** "See `docs/MODELS.md` for the confidence
  bands" is a pointer. "See `docs/MODELS.md` — the default is 0.5" is a
  second copy of the fact, and it will drift.
- **When behavior changes, grep every mention** and fix the owner; verify
  the pointers still read true.
- **Prefer deleting to duplicating.** Moving a section is better than
  summarizing it in a second place.
### Anti-drift in code

The same rule, and it fails the same way: a behavior implemented twice
diverges, and nothing signals which copy is right. `human_labels.py`,
`model_usage.py`, and `ranking.py` exist because two surfaces needed the
same logic — that is the shape to reach for.

- **Grep the behavior, not the name, before writing a helper.** Names don't
  match across authors: `_output_folder` would never have found the existing
  `_find_existing_output`. Grep what it *does* — `job_id[:8]`, `split("|")`,
  the column being written.
- **One constant, one definition.** A comment reading "matches
  `other._THING`" is drift documented as drift; import it instead.
- **Two callers means extract, not copy.** When a second surface needs
  existing logic, move it somewhere both can import — even if that means a
  new small module. A justification for copying ("avoids a heavy import")
  usually means the code is in the wrong place, not that it should exist
  twice.
- **Adapters, never parallel implementations.** Slack, the CLI, and
  `script.sh` are entry points over shared logic. An entry point that
  reimplements a transition is a defect, however well it works.
- **Check for duplication as part of every audit** — it is a named step, not
  something to notice. Ask: what did this change add that already existed
  somewhere?
- **Register what you extract.** A source-level test asserting one concept →
  one implementation site is the only layer that survives forgetting; see
  `TestMeteringCoverage` in `tests/test_model_usage.py` for the pattern.
  Add the entry when you extract, while the decision is fresh.

### Writing for each audience

- **README** — human-readable and brief. ASCII diagrams over prose where a
  picture is clearer; prose over tables where it reads better. Aim to be
  attractive to someone deciding whether to use this. Cut anything a reader
  doesn't need *at that moment*; push the detail into `docs/`.
- **CLAUDE.md** — written for a coding agent with no memory of this repo.
  Favor the non-obvious: invariants, load-bearing behavior, the trap that
  looks like a bug. Skip anything the code already states plainly.
- **Interfaces are one surface.** A verb that is correct alone but tells a
  different story than its neighbors is a defect. Render every affected
  command on real data before calling interface work done.

### Audit checklist

When auditing your own or prior work, these are steps rather than instincts:

1. **Duplication** — what did this add that already existed? Grep the
   behavior, not the name.
2. **Coherence** — render every affected command on real data; do the
   surfaces tell one story?
3. **Drift** — grep every doc mentioning the changed behavior; fix the
   owner, verify pointers still read true.
4. **Claims** — re-check assertions against the code, including ones made
   earlier in the same session.

## Conventions

- Multi-line LLM prompt templates in `triage.py` / `tailor.py` are deliberately one prose sentence per line so the wire-format is preserved — do not reflow them; ruff E501 is already ignored for these files.
- Several entry points need `load_dotenv()` before importing modules that read env at import time → E402 is ignored for those (`pipeline.py`, `digest.py`, `batch_process.py`, `jobspy_ingest.py`, `process_queue.py`, `proxy_test.py`, `sweeper.py`, `tailor.py`).
- Squash on merge; commit messages on `main` read like changelog entries.
- **Slack thread commands (`!applied`, `!triage`, `!trend`, …) are frozen** — they still work, but new post-triage functionality belongs in the CLI review surface, not `sweeper.py`. Slack *reactions* (👍 👎 ✏️ ❓) are unaffected. See `docs/CHATOPS.md`.
- **Every human decision must append to `data/human_labels.jsonl`** via `src/human_labels.py::append_human_label` (pass `surface=`). That ledger is the only input to the preference-pair extractor behind the ranking evals — a surface that skips it is invisible to that work, silently.
- **Metered spend must be auditable.** Any code path that calls OpenRouter routes its token count through `src/model_usage.py::log_model_usage` (model, stage, tokens — never prompt or response content). `logs/model_usage.log` is the audit trail and the basis for spend ceilings; a spending path missing from it makes the budget unenforceable. Currently only `triage.py` complies — `tailor.py`, `research.py`, and `process_queue.py` are known gaps. In-session (subscription-billed) work is exempt: it has no metered cost to record.
