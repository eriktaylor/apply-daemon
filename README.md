# apply-daemon

**Stop scrolling job boards. Triage your job hunt from chat.**

**apply-daemon** is an open-source pipeline that automates the job search marathon: it monitors target roles, evades scraper blocks, scores every listing against your `profile.md` with cascading LLMs, and pre-researches the best matches — so reviewing your day's candidates takes minutes, and tailoring a resume takes one command.

Two tracks feed it; three ways to drive it:

- **Track A** — JobSpy automates LinkedIn and Indeed search.
- **Track B** — email ingestion: job alerts, board digests, Google Alerts.

| Entry point | What it's for |
|---|---|
| `./script.sh` | The daily batch — scrape, score, enrich. Run it when you want fresh listings. |
| `python -m src.cli` | Review and decide. A bundled Claude Code skill drives it conversationally; resume tailoring runs in-session at zero API cost. |
| Slack | Ambient: daily digest cards, triageable from your phone with four reactions. Optional. |

**Tech stack:** Python · OpenRouter · Claude Code · Slack · JobSpy · Gmail · IPRoyal

## Setup checklist

Work through these once during onboarding, in order. Each item maps to a section below.

- [ ] **A. Update your resume** (e.g. polish bullets with [claude.ai](https://claude.ai))
- [ ] **B. Clone the repository**
- [ ] **C. Install dependencies**
- [ ] **D. Set up the Slack channel and bot** *(optional — skip for CLI-only use)*
- [ ] **E. Configure OpenRouter (required) and your `.env`**
- [ ] **F. Configure `profile.md` (required), `search_config.yaml` (Track A), and/or email alerts (Track B)**
- [ ] **G. Configure an IPRoyal residential proxy for heavy scraping (optional)**
- [ ] **H. Run the pipeline**

## How it works

### Ingestion & scoring — two tracks, one store

```
Track A (Proactive)                      Track B (Reactive)
─────────────────────────────            ──────────────────────────────────
JobSpy scrape_jobs() → DataFrame         IMAP fetch → Email classifier
        │                                        │
Stage 4: Structured map (no LLM)         Stage 1: LLM anchor extraction
Stage 4b: Lazy-load full description     Stage 2: Field validation
        │  (if truncated by board)       Stage 3: Scrape + DDGS heal
        │                                  (speculative synthesis fallback)
        └──────────────┬─────────────────────────┘
                       │
               Dedup check (pre-LLM)  ← fuzzy match against DB;
               already known? → skip    skip Stage 5 entirely
                       │
               Stage 5: LLM scoring (confidence threshold)
                       │
               db.upsert_listing()   ← Smart Upsert: UPDATE if fuzzy-
               (fuzzy match; UPDATE    matched, INSERT if new
                if exists, INSERT if new)
```

**Track A** polls job boards directly via JobSpy and injects structured listings without any LLM extraction. **Track B** is the reactive pipeline — email alerts, Google Alerts, and Slack `!triage` commands — which uses LLM anchor extraction for emails that only contain free-form text. Both tracks share Stage 5 scoring and the same SQLite database; the Smart Upsert ensures no duplicates regardless of which track found the listing first.

Details the diagram doesn't show:

- **Track A** (`jobspy_ingest.py`) reads `my_profile/search_config.yaml` and scrapes each search × site-tier pair; **Stage 4b** fetches the full description whenever a preview is under 300 words or truncated.
- **Track B** classifies email by headers alone (JOB_DIGEST / RECRUITER_OUTREACH / GOOGLE_ALERT / SKIP — no LLM cost) and extracts text generically, with no per-platform parsers.
- **Dedup is fuzzy and pre-LLM** (`rapidfuzz` token-set, 85%): known listings are skipped before any API call; a Smart Upsert after scoring handles races between tracks.
- **Stage 5 triage** returns YES / MAYBE / NO with 0–100 confidence; NO always drops, YES/MAYBE survive above `CONFIDENCE_THRESHOLD`, with skills match and reasoning attached.
- **Enrichment**: Nominatim commute distance from `home_location`, plus a repost timeline when a listing has been seen before.

### The daily loop

Same four gestures every listing, every day — the repetition is the point:

```
$ ./script.sh --dry-run                 # what would a run cost?
$ ./script.sh                           # scrape → score → enrich (budget-gated)

$ python -m src.cli status              # 43 fresh of 490 · $0.00 of $3.00 today
$ python -m src.cli next                # top 3, best match first

  [1] Staff ML Engineer — Acme             YES 95%  ·  auto  ·  Local  ·  2d
  [2] ...

$ python -m src.cli deep-dive <id>      # Stage 5 vs post-research verdict + research
$ python -m src.cli save <id>           # or: pass <id> · pass --all
$ python -m src.cli tailor <id>         # resume tailored in-session, no API cost
$ python -m src.cli next                # next 3…
```

Or just ask Claude Code *"anything good today?"* — the bundled skill runs
these verbs for you and reports what each step costs.

> **Still worth a manual click:** the CLI doesn't yet check whether a posting
> is still live, so open the URL before investing in a tailor — especially on
> listings more than a couple of weeks old.

### Review & apply — one store, two surfaces

Everything above converges on SQLite. Everything below fans back out of it:

```
                     db.upsert_listing()
                              │
                Autopilot (process_queue.py)
                top-N Deep Research + re-score → status 'auto';
                research dossier cached in output/…
                              │
        ┌─────────────────────┴─────────────────────────┐
        │                                               │
Slack — ambient surface                  CLI + Claude skill — work surface
───────────────────────────              ──────────────────────────────────
digest.py → Block Kit card               python -m src.cli <verb> --json
reactions: 👍 save · 👎 pass               next       → top 3 (auto tier
           ✏️ tailor · ❓ route                          first: deep-dive
sweeper.py polls + backstops                           is token-free)
thread ChatOps: frozen                   deep-dive  → Stage 5 vs post-
        │                                             research verdict,
        │                                             delta, dossier
        │                                save / pass / pass --all
        │                                next       → 3 more…
        │                                       │
        └─────────────────────┬─────────────────┘
                              │
              SQLite — single source of truth
              pipeline_status + presented_at  ← paging state, not a
                              │                 gate: undecided rows
                              │                 reappear next session
              data/human_labels.jsonl  ← every decision, both surfaces
                                         → preference pairs → ranking evals
```

**Slack** is the ambient surface — the daily digest and four reactions, triageable from a phone. **The CLI** is the work surface: a Claude Code skill drives it conversationally — show the top 3, deep-dive one, pass the rest, pull 3 more. **Both** write the same two records (`pipeline_status` and a labeled decision in `human_labels.jsonl`), so the surfaces can't drift and every decision becomes ranking training data.

Autopilot sits above the fork because its enrichment serves both surfaces — and it's why deep-diving an `auto`-tier listing costs no tokens: the research is already on disk. The CLI's `deep-dive` shows the Stage 5 score *and* the post-research re-score side by side; they routinely disagree, and that gap is the most decision-relevant thing about a listing.


## Setup

### A. Update your resume

The pipeline tailors a single `base_resume` document for every saved listing, so the strength of your starting resume sets the ceiling on every downstream tailor. Polish your bullets first — [claude.ai](https://claude.ai) is a good thinking partner for this — and have the file ready before step F.

Supported formats are `.docx`, `.md`, and `.pdf`, resolved in that priority order.

| File | Purpose |
|---|---|
| `base_resume` (.docx / .md / .pdf) | Required for resume tailoring. The LLM edits bullets against this document. |
| `cover_letter` (.docx / .md / .pdf) | Optional style reference — the LLM mimics its tone, so it should be well-written. No template? Use on-demand generation (`!coverletter`) instead, which writes from profile + resume + research. |

### B. Clone the repository

```bash
git clone <repo-url>
cd apply-daemon
cp -r my_profile_example my_profile
cp .env.example .env
```

> **`my_profile/` is gitignored** — your customizations stay local and never collide with `git pull`. To pick up template changes from the upstream repo, diff `my_profile_example/` against your copy.

### C. Install dependencies

```bash
# Using uv (recommended)
# uv automatically creates the virtual environment and syncs dependencies from pyproject.toml
uv sync && source .venv/bin/activate

# Or using pip (legacy)
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

### D. Slack channel and bot (optional)

Powers the digest cards and reaction triage. **Skip it for CLI-only use** — ingestion and the review CLI run fine without it (the digest step just logs a warning).

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps) → "From scratch"; add Bot Token Scopes `chat:write`, `channels:history`, `reactions:read`, `reactions:write`; install to your workspace.
2. Copy the `xoxb-...` Bot Token, and the Channel ID (`C...`) from your target channel's details.
3. Invite the bot: `/invite @YourBotName` in the channel (skipping this causes the common `not_in_channel` error).
4. Paste both into `.env` in step E: `SLACK_BOT_TOKEN=...`, `SLACK_CHANNEL_ID=...`

### E. OpenRouter and `.env`

Fill in your `.env` — every variable is documented inline in [`.env.example`](.env.example). The non-obvious ones:

- **OPENROUTER_API_KEY** *(required)* — powers the pipeline's LLM calls. Get a key at [openrouter.ai/keys](https://openrouter.ai/keys). Per-stage model slots, defaults, and BYOK setup: [`docs/MODELS.md`](docs/MODELS.md).
- **SLACK_BOT_TOKEN** / **SLACK_CHANNEL_ID** — From step D.
- **GMAIL_ADDRESS** / **GMAIL_APP_PASSWORD** — Required only if you plan to use Track B email ingestion (step F). Create a dedicated Gmail account for job alerts, enable 2FA, and generate an [App Password](https://support.google.com/accounts/answer/185833).
- **CONFIDENCE_THRESHOLD** — minimum Stage 5 confidence to keep a listing. Bands and migration notes: [`docs/MODELS.md`](docs/MODELS.md).

Runtime knobs that don't belong in `profile.md` (model slots, `CONFIDENCE_THRESHOLD`, `GENERATE_ASSETS`, Slack tokens, IPRoyal credentials) all live in `.env`.

### F. Profile, search config, and/or email alerts

Pick at least one of Track A or Track B. `profile.md` is required for both.

#### `profile.md` (required)

Edit `my_profile/profile.md` — write naturally about who you are, what you want, and what you don't want. The LLM reads it like a person would. Richer descriptions produce better matching. Drop your `base_resume` (and optional `cover_letter`) from step A into `my_profile/` alongside it.

The **Pipeline Settings** table in `profile.md` (e.g. `max_listings_per_run`, `dedup_window_days`, `pass_window_days`, `batch_process_days`, `home_location`, `max_listing_age_days`) controls runtime behaviour. See [`my_profile_example/profile.md`](my_profile_example/profile.md) for the full set of values and inline notes.

Deep Research runs automatically before autopilot re-scores and ✏️/`--via api` tailoring; in-session tailoring reuses the cached dossier instead of spending.

#### `search_config.yaml` — Track A (JobSpy proactive search)

Edit `my_profile/search_config.yaml`. The shipped template at `my_profile_example/search_config.yaml` is a generic Machine Learning / AI Engineer starting point — open it and tailor the `search_term`, `location`, and tier `results_wanted` values to your job hunt.

The config has two top-level sections that the inline comments document in full:

- **`site_tiers`** — boards grouped by scraping reliability (`friendly` / `ok` / `hostile`). Set `results_wanted: 0` to disable a tier without deleting it.
- **`searches`** — one entry per search term × location. Every entry is run against every enabled tier, so `N searches × M active tiers` queries execute per run.

A `delays` block randomizes the gap between queries (default 7–20 s) to avoid IP bans, and an env-driven `# PROXY (OPTIONAL)` comment block at the bottom of the file documents the IPRoyal integration. Results from all searches are deduplicated against each other and against any listings already in the database from Track B emails.

> **LinkedIn:** `linkedin_fetch_description=True` is passed automatically when LinkedIn is included in a tier, fetching full job descriptions at scrape time instead of relying on the lazy-loader. **Indeed:** Truncated search-result previews trigger Stage 4b, which scrapes the `indeed.com/viewjob?jk=...` detail page for the full posting.

#### Email alerts — Track B

Track B reads from a dedicated Gmail inbox over IMAP. Point your existing job alert subscriptions (LinkedIn job alerts, Indeed saved-search digests, Google Alerts on `"<role> jobs"`, recruiter newsletters) at the dedicated address you set in `GMAIL_ADDRESS` (step E). The pipeline classifies each unread message by header (JOB_DIGEST / RECRUITER_OUTREACH / GOOGLE_ALERT / SKIP) and only the first three are processed.

### G. Rotating residential proxy (optional)

If you scrape LinkedIn aggressively, run multiple proactive cycles per day, or aim deep-research scrapes at hardened ATS pages, your home IP will eventually trip Cloudflare / DataDome / LinkedIn's auth wall. Apply Daemon integrates first-class with [IPRoyal](https://iproyal.com/) sticky residential sessions for these cases.

See [`docs/PROXY.md`](docs/PROXY.md) for setup, rotation behaviour, and the smoke-test workflow.

### H. Run the pipeline

Activate the virtualenv first if it isn't already (`source .venv/bin/activate`), then continue below.

**Integration evaluation:**

```bash
python -m src.integration_test
```

Walks the checklist, reporting which components are configured and reachable. Indicates a go-ahead for Track A, Track B, or both.

> **Designed to consume the absolute minimum of paid credits** — the only billable call is the single OpenRouter token. Pass `--no-llm` to skip even that, or `--no-network` to skip every remote check.

**Daily batch (recommended):**

```bash
./script.sh              # budget-gated run of both tracks + autopilot
./script.sh --dry-run    # show the stages and budget verdict, run nothing
./script.sh --top-n 5    # raise autopilot enrichment for this run only
```

`script.sh` is a thin wrapper over `python -m src.cli refresh`, which owns the stage sequence and checks your spend ceiling first (see `DAILY_USD_BUDGET` and `MIN_RUN_INTERVAL_MINUTES` in `.env.example`). It refuses rather than half-running, reports what the run cost, and exits non-zero if a stage fails. Then review with `python -m src.cli next` — or `python -m src.sweeper` if you triage from Slack.

**Manual run:**

```bash
# Track A
python -m src.jobspy_ingest && python -m src.digest

# Track B
python -m src.pipeline && python -m src.digest

# Sweep Slack reactions and ChatOps commands
python -m src.sweeper
python -m src.sweeper --deep 99  # Scan last 99 posts; default is 50

# Batch tailor every saved listing (concurrent OpenRouter calls)
python -m src.batch_process

# Autopilot (needs AUTOPILOT_ENABLED=true): Deep Research + re-score for the
# top-N queued listings; caches the dossier so later deep-dives/tailors are free
python -m src.process_queue
python -m src.process_queue --backfill   # first enable? promote existing YES/MAYBE

# Funnel report
python -m src.report             # All-time reference
python -m src.report --days 7    # Last 7 days reference

# CLI review surface — triage without Slack. Add --json for scripting.
python -m src.cli status         # Queue freshness + today's spend vs budget
python -m src.cli refresh        # Run the pipeline (--dry-run / --top-n N / --force)
python -m src.cli next --top 3   # Next page of candidates
python -m src.cli deep-dive <id> # Stage 5 vs post-research verdict + dossier
python -m src.cli save <id>      # or: pass <id> / pass --all
python -m src.cli tailor <id>    # Tailor in-session (--via api to spend)

# One-time: geocode locations so the queue can sort by distance
python -m src.geo_backfill --dry-run
```

The CLI reads `$APPLY_DAEMON_DB` (falling back to `./apply_daemon.db`), so it works from any directory. Showing a listing never consumes it — undecided listings return to the queue after a two-hour window.

**Slack reactions:** 👍 save · 👎 pass · ✏️ tailor · ❓ smart-route, directly on a digest card. Priority, idempotency, and every thread command are documented in [`docs/CHATOPS.md`](docs/CHATOPS.md).

> **Output:** tailored assets land in `output/<Company>_<Title>_<ID>/` — ready-to-send `.docx` files plus a JSON dump of the LLM response.

## ChatOps & Commands

Post-triage work happens on two surfaces.

**The CLI** (`python -m src.cli`, command list in step H) is where new work goes. A bundled Claude Code skill (`.claude/skills/apply-daemon/`) drives it conversationally: ask Claude "what's new?" and it walks you through the top matches, deep-dives whichever you pick, and records your decisions. Reviewing never spends tokens — enrichment is pre-cached by autopilot, and in-session tailoring is billed to your Claude session, not an API.

**Slack** is the ambient surface: the digest plus four reactions, processed by `python -m src.sweeper`. Thread commands are **frozen** — new verbs land in the CLI — but three things are still Slack-only today, so a full application often ends there:

| Still Slack-only | Command |
|---|---|
| Polish a tailor run you didn't like | `!polish` |
| Cover letter | `!coverletter` |
| Answer custom application questions | `!answer` |

Full reference: [`docs/CHATOPS.md`](docs/CHATOPS.md).

## Running tests

```bash
ruff check . && pytest    # exactly what CI runs
```

## Eval harness

Test extraction + matching accuracy on labeled emails:

```bash
python -m eval.eval --input eval/eval_example.csv --model google/gemini-3.1-flash-lite
```

## Security

See [`SECURITY.md`](SECURITY.md) for the full security policy, threat
model, contributor mantra, and vulnerability disclosure process.

Quick summary:

- **Never commit** `.env`, `*.db`, `my_profile/`, or any `my_profile_*/`
  variant other than the synthetic `my_profile_example/`.
- Test fixtures use synthetic data only.
- Logging outputs listing IDs and decisions — never raw email content,
  credentials, or LLM prompts/responses.

## Roadmap

Shipped features are catalogued in [`CHANGELOG.md`](CHANGELOG.md).

### Up Next

- [ ] **The Command Center GUI (Next.js)** — A lightweight local web dashboard that connects to the SQLite DB to visualize the full application funnel (ingested → triaged → saved → tailored → applied). Provides an interface to review and curate the `human_labels.jsonl` dataset for future model fine-tuning. Triage stays in chat/Slack; management and analytics move to this GUI.
- [ ] **The Dynamic RAG "Brag Document"** — Shift from editing a single `base_resume.docx` to dynamic assembly. A massive `master_brag_document.md` stores every bullet, project, and achievement. The pipeline semantically searches this document against the job description, pulling only the top most relevant bullets for the LLM. Eliminates hallucinations and produces hyper-targeted resumes.
- [ ] **The "Warm Intro" API (Cold Outreach Copilot)** — Repurpose cold outreach into a bridge feature. Uses Deep Research context to autonomously draft a highly targeted, 3-sentence DM. Exposed via a central API endpoint so it can be routed to the user's Slack for manual LinkedIn messaging, or eventually piped into a partner ATS/recruiter dashboard.

### Future / Icebox

- [ ] **"Hosted Receipt" Verification** — Generate public, read-only web links of `deep_research_context.txt` to prove the application was AI-researched with real company data.
- [ ] **Interactive Mock Interview Agent** — A Slack command (`/interview`) triggering an agent to act as the hiring manager in a threaded conversation, testing technical fit before the real interview.
- [ ] **Headless Auto-Apply via `browser-use`** — Navigate ATS portals autonomously to submit applications. Currently iceboxed due to brittleness from constant DOM changes across ATS platforms.
