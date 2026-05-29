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
python -m src.batch_process     # Concurrent tailor for all saved listings
python -m src.process_queue     # Autopilot Speculative Agent (no-op unless AUTOPILOT_ENABLED=true)
python -m src.process_queue --backfill        # Promote existing YES/MAYBE into autopilot queue
python -m src.integration_test  # Config + reachability check (use --no-llm / --no-network to skip)
python -m src.report --days 7   # Funnel metrics
```

## Architecture

Two ingestion tracks converge on a shared LLM scoring stage and a single SQLite store (`apply_daemon.db`). All LLM calls route through **OpenRouter**; Slack is the only UI.

```
Track A (jobspy_ingest.py)              Track B (pipeline.py)
  scrape_jobs() → structured rows         IMAP → classify → extract text
  Stage 4b: lazy-load full description    Stage 1: LLM anchor extraction
  (when preview < 300 words or truncated) Stage 2-3: validate, scrape, DDGS heal
                       │                                 │
                       └────────── dedup (rapidfuzz token-set, 85%) ──────────┘
                                          │
                          Stage 5: triage.py LLM scoring
                          (verdict YES/MAYBE/NO + 0–100 confidence,
                           gated by CONFIDENCE_THRESHOLD)
                                          │
                                  db.upsert_listing()
                                  (Smart Upsert: fuzzy match → UPDATE, else INSERT)
                                          │
                                  digest.py → Slack Block Kit card
                                          │
                                  Reactions handled by sweeper.py
                                  (👍 save, 👎 pass, ✏️ tailor, ❓ smart-route)
                                          │
                                  tailor.py + research.py + compile.py
                                  → output/<Company>_<Title>_<ID>/
```

**Dedup happens *before* Stage 5** — already-known listings are skipped without spending OpenRouter tokens. The Smart Upsert at the end handles races between the two tracks.

**Three independent OpenRouter model slots** (`OPENROUTER_STAGE1_MODEL`, `OPENROUTER_MODEL` (Stage 5), `OPENROUTER_TAILOR_MODEL`, plus optional `OPENROUTER_TREND_MODEL`) let cost/quality be tuned per stage. See `docs/MODELS.md`.

**Autopilot (Speculative Agent)** — when `AUTOPILOT_ENABLED=true`, `process_queue.py` runs Deep Research + a Claude match-analysis pass on every listing the pipeline auto-queues, posts the enriched card, and auto-passes post-research NO verdicts. Manual ✏️ tailoring reuses the cached research.

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
├── docs/                        # CHATOPS.md, MODELS.md, PROXY.md, EVAL_GUIDE.md, PROJECT_BRIEFING.md
├── script.sh                    # Daily batch chain (jobspy_ingest → digest → pipeline → digest → process_queue)
├── pyproject.toml               # Direct dependencies + loose version constraints
├── requirements.lock            # Autogenerated full resolution (uv pip compile)
└── apply_daemon.db              # SQLite store (GITIGNORED)
```

### Configuration split

- **`.env`** — secrets + runtime knobs (model slots, `CONFIDENCE_THRESHOLD`, `GENERATE_ASSETS`, `AUTOPILOT_ENABLED`, Slack/Gmail/IPRoyal creds).
- **`my_profile/profile.md`** — candidate profile + Pipeline Settings table (`max_listings_per_run`, `dedup_window_days`, `home_location`, etc.). Gitignored.
- **`my_profile/search_config.yaml`** — Track A only: `site_tiers` (friendly/ok/hostile) × `searches`. Runs N searches × M active tiers per cycle.
- **`my_profile_example/`** is the synthetic template; `my_profile/` is the user's gitignored copy.

## Security ground rules (from SECURITY.md)

- Never commit `.env`, `*.db`, `my_profile/`, or any `my_profile_*/` variant other than `my_profile_example/`.
- Test fixtures must be synthetic — no real listings, real emails, or real credentials.
- Logging must emit listing IDs + decisions only — **never raw email content, LLM prompts/responses, or credentials.**
- Don't weaken `.gitignore`, disable TLS verification, or add raw-content logging.

## Conventions

- Multi-line LLM prompt templates in `triage.py` / `tailor.py` are deliberately one prose sentence per line so the wire-format is preserved — do not reflow them; ruff E501 is already ignored for these files.
- Several entry points need `load_dotenv()` before importing modules that read env at import time → E402 is ignored for those (`pipeline.py`, `digest.py`, `batch_process.py`, `jobspy_ingest.py`, `process_queue.py`, `proxy_test.py`, `sweeper.py`, `tailor.py`).
- Squash on merge; commit messages on `main` read like changelog entries.
