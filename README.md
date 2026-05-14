# apply-daemon

A local-first job search automation pipeline that monitors job alert emails, triages listings against a candidate profile using an LLM via OpenRouter, and surfaces the best matches.

## How it works

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

1. **Track A — Proactive polling** (`src/jobspy_ingest.py`) — Reads `my_profile/search_config.yaml`, calls `scrape_jobs()` across Indeed and LinkedIn (configurable per site tier) for each search × tier pair. Returns a pandas DataFrame with structured fields (title, company, location, salary, full description, URL). No LLM extraction needed — maps directly to Stage 5 scoring. **Stage 4b** lazy-loads the full job description from the ATS or Indeed detail page whenever the scraped preview is under 300 words or ends with a truncation marker.
2. **Track B — Reactive email pipeline** — Connects to a dedicated Gmail inbox via IMAP and pulls unread job alert emails from LinkedIn, Google Alerts, and other sources.
3. **Email classification** — Fast regex-based classification (JOB_DIGEST / RECRUITER_OUTREACH / GOOGLE_ALERT / SKIP) using headers only. No LLM cost.
4. **Text extraction** — Generic, template-agnostic HTML-to-text conversion via BeautifulSoup. Works on any email from any platform — no platform-specific parsers.
5. **Dedup** — Fuzzy dedup using `rapidfuzz` token-set-ratio (85% threshold) at the email level and again per-anchor **before Stage 5 LLM scoring**. Listings already in the database are skipped immediately — no OpenRouter API calls are made for known jobs. A final Smart Upsert after scoring handles any races between tracks.
6. **LLM triage** — Single-model scoring against your candidate profile with a configurable confidence threshold. `OPENROUTER_MODEL` returns a verdict (YES / MAYBE / NO) and a 0–100 confidence. NO verdicts are always dropped; YES / MAYBE are kept only when confidence meets `CONFIDENCE_THRESHOLD`. Returns structured data: title, company, location, salary, verdict, confidence, skills match, and reasoning.
7. **Geo distance** — Calculates commute distance from your `home_location` to each job using OpenStreetMap Nominatim geocoding with LRU caching.
8. **Historical context** — Detects reposted listings via fuzzy matching and surfaces a timeline of prior encounters in the digest.
9. **Storage + notification** — Results saved to SQLite. Daily Slack digest with Block Kit formatting, skills match percentages, geo distance, and history context. Rate-limited with retry handlers and inter-message pacing.

## Setup

### 1. Clone and configure

```bash
git clone <repo-url>
cd apply-daemon
cp -r my_profile_example my_profile
cp .env.example .env
```

Edit `my_profile/profile.md` — write naturally about who you are, what you want, and what you don't want. The LLM reads it like a person would. Richer descriptions produce better matching.

Drop your files into `my_profile/` — supported formats are `.docx`, `.md`, and `.pdf`, resolved in that priority order:

| File | Purpose |
|---|---|
| `base_resume` (.docx / .md / .pdf) | Required for resume tailoring. The LLM edits bullets against this document. |
| `cover_letter` (.docx / .md / .pdf) | Optional style reference. The LLM matches your preferred tone and structure. |

The **Pipeline Settings** table in `profile.md` (e.g. `max_listings_per_run`, `dedup_window_days`, `pass_window_days`, `batch_process_days`, `home_location`) controls runtime behaviour. See [`my_profile_example/profile.example.md`](my_profile_example/profile.example.md) for the full set of values and inline notes.

Runtime knobs that don't belong in `profile.md` (model slots, `CONFIDENCE_THRESHOLD`, `GENERATE_ASSETS`, Slack tokens, IPRoyal credentials) live in `.env`. See [`.env.example`](.env.example) for every variable with inline comments.

Deep Research is always enabled and runs before every Tailor operation.

### 2. Set up credentials

Fill in your `.env` — every variable is documented inline in [`.env.example`](.env.example). The non-obvious ones:

- **GMAIL_ADDRESS** / **GMAIL_APP_PASSWORD** — Create a dedicated Gmail account for job alerts. Enable 2FA and generate an [App Password](https://support.google.com/accounts/answer/185833).
- **OPENROUTER_API_KEY** — Required for all LLM calls. Get your key from [openrouter.ai/keys](https://openrouter.ai/keys).
- **CONFIDENCE_THRESHOLD** (optional, default `0.5`) — Minimum Stage 5 confidence (0.0–1.0) required to keep a listing. Set to `0.0` to disable auto-rejection, or `0.75`+ for stricter filtering. See [`docs/MODELS.md`](docs/MODELS.md) for how the threshold also gates the AUTO_MATCH band.

### 3. Install dependencies

```bash
# Using uv (recommended)
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

# Or using pip
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

> **Upgrading from an earlier version?** Run `pip install -e ".[dev]"` again to pick up the two new dependencies: `python-jobspy` and `pyyaml`.

### 4. Configure JobSpy proactive search (Track A)

Edit `my_profile/search_config.yaml` (created when you ran `cp -r my_profile_example my_profile`). The shipped template at `my_profile_example/search_config.yaml` is a generic Machine Learning / AI Engineer starting point — open it and tailor the `search_term`, `location`, and tier `results_wanted` values to your job hunt.

The config has two top-level sections that the inline comments document in full:

- **`site_tiers`** — boards grouped by scraping reliability (`friendly` / `ok` / `hostile`). Set `results_wanted: 0` to disable a tier without deleting it.
- **`searches`** — one entry per search term × location. Every entry is run against every enabled tier, so `N searches × M active tiers` queries execute per run.

A `delays` block randomizes the gap between queries (default 7–20 s) to avoid IP bans, and an env-driven `# PROXY (OPTIONAL)` comment block at the bottom of the file documents the IPRoyal integration. Results from all searches are deduplicated against each other and against any listings already in the database from Track B emails.

> **`my_profile/` is gitignored** — your customizations stay local and never collide with `git pull`. To pick up template changes from the upstream repo, diff `my_profile_example/search_config.yaml` against your copy.

> **LinkedIn:** `linkedin_fetch_description=True` is passed automatically when LinkedIn is included in a tier, fetching full job descriptions at scrape time instead of relying on the lazy-loader. **Indeed:** Truncated search-result previews trigger Stage 4b, which scrapes the `indeed.com/viewjob?jk=...` detail page for the full posting.

#### Optional: Rotating residential proxy (recommended for heavy scraping)

If you scrape LinkedIn aggressively, run multiple proactive cycles per day, or aim deep-research scrapes at hardened ATS pages, your home IP will eventually trip Cloudflare / DataDome / LinkedIn's auth wall. Apply Daemon integrates first-class with [IPRoyal](https://iproyal.com/) sticky residential sessions for these cases.

See [`docs/PROXY.md`](docs/PROXY.md) for setup, rotation behaviour, and the smoke-test workflow.

### 5. Slack notifications

The digest, sweeper, and reaction workflow all run through Slack. Set this up before running the pipeline.

1. **Create a Slack app** at [api.slack.com/apps](https://api.slack.com/apps) → "Create New App" → "From scratch".
2. **Add bot token scopes** — Go to "OAuth & Permissions" → "Bot Token Scopes" → Add: `chat:write`, `channels:history`, `reactions:read`, `reactions:write`.
3. **Install the app** to your workspace — Click "Install to Workspace" and authorize.
4. **Copy the Bot Token** — After install, copy the `xoxb-...` token from "OAuth & Permissions".
5. **Get the channel ID** — In Slack, right-click your target channel → "View channel details" → copy the Channel ID (starts with `C`).
6. **Invite the bot to the channel** — In the Slack channel, type: `/invite @YourBotName`
7. **Set your `.env`**:
   ```
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   SLACK_CHANNEL_ID=C0123456789
   ```

> **Common error:** If you see `not_in_channel`, the bot hasn't been invited. Run `/invite @YourBotName` in the channel.

### 6. Run the pipeline

**Manual test run:**

```bash
# Track A — Proactive polling: scrape job boards for all configured searches
python -m src.jobspy_ingest   # or: apply-daemon-ingest

# Track B — Reactive pipeline: process new email alerts
python -m src.pipeline        # or: apply-daemon

# Fire the digest — reads triaged jobs from the DB and posts a Block Kit summary to Slack
python -m src.digest          # or: apply-daemon-digest

# Run the sweeper — scans Slack for reactions and ChatOps commands
python -m src.sweeper         # or: apply-daemon-sweeper

# Run the batch processor — concurrent OpenRouter tailor requests for all saved listings
python -m src.batch_process   # or: apply-daemon-batch

# Run the sweeper — scans Slack for reactions and ChatOps commands
python -m src.sweeper           # or: apply-daemon-sweeper
python -m src.sweeper --deep 99 # Scan last 99 posts, default is 50

# Run the funnel report — actionable batch vs reference period metrics
python -m src.report            # All-time reference
python -m src.report --days 7   # Last 7 days reference
```

> **Automation:** Use cron to further automate runs at desired timing.

**How reactions work:**

Each digest message includes a reaction legend. React directly on a card to drive its state — no buttons or Socket Mode required.

| Reaction | Action | Result |
|----------|--------|--------|
| :thumbsup: | **Save** | Status → `saved`, bot adds :white_check_mark: receipt |
| :thumbsdown: | **Pass** | Status → `passed`, message replaced with gray "Passed" |
| :pencil2: | **Tailor** | Runs Deep Research + LLM, generates targeted resume + match analysis |
| :grey_question: | **Smart Router** | Routes to tailor or custom-answer fast-path depending on context |

Reaction priority is `pass` > `tailor` > `save`, and a sweeper-level idempotency layer prevents duplicate firing. For the full reaction priority semantics, ChatOps thread commands (`!applied`, `!coverletter`, `!regenerate`, `!triage`, `!update`, `!trend`, etc.), the Smart Router routes, and the threaded scrape-failure recovery workflow, see [`docs/CHATOPS.md`](docs/CHATOPS.md).

> **Output directory:** Tailored assets (targeted resume, match analysis, and on-demand cover letter/interview prep) are saved locally to `output/<Company>_<Title>_<ID>/`. Each job gets its own directory with ready-to-send `.docx` files and a JSON dump of the full LLM response.

## ChatOps & Commands

The post-triage workflow runs entirely on Slack reactions and thread commands, swept every 2 minutes. State-tracking commands (`!applied`, `!pass`, `!interview`, `!rejected`), on-demand asset generation (`!coverletter`, `!prep`, `!polish`), regeneration (`!regenerate`), the Smart Router (`❓` / `!answer`), manual ingestion (`!triage <URL>`) with its threaded scrape-failure recovery (`!update`), and the labor-market intelligence command (`!trend`) are all documented in [`docs/CHATOPS.md`](docs/CHATOPS.md).

## Model Selection & Confidence Threshold

All LLM calls route through [OpenRouter](https://openrouter.ai). Three independent model slots (`OPENROUTER_STAGE1_MODEL`, `OPENROUTER_MODEL`, `OPENROUTER_TAILOR_MODEL`, plus an optional `OPENROUTER_TREND_MODEL`) let you optimise cost and quality per pipeline stage, and `CONFIDENCE_THRESHOLD` (0.0–1.0) sets the minimum LLM confidence required to keep a listing in Stage 5.

See [`docs/MODELS.md`](docs/MODELS.md) for the full per-slot defaults, Anthropic BYOK setup, the confidence-threshold bands (50 / 55–75 / 80%), and how the eval scripts interact with each.

## Running tests

```bash
pytest
```

## Eval harness

Test extraction + matching accuracy on labeled emails:

```bash
python -m eval.eval --input eval/eval_example.csv --model google/gemini-3.1-flash-lite-preview
```

## Project structure

```
apply-daemon/
├── my_profile_example/          # Template — cp -r to my_profile/
│   ├── profile.example.md
│   ├── base_resume.docx
│   ├── cover_letter.md
│   └── search_config.yaml       # JobSpy search config (Track A) — generic ML/AI engineer starter
├── my_profile/                  # Your data + customized search_config.yaml (gitignored)
├── src/
│   ├── jobspy_ingest.py       # Track A — Proactive polling via JobSpy
│   ├── pipeline.py            # Track B — Silent worker (fetch, triage, store)
│   ├── digest.py              # Slack digest (posts listings for reaction)
│   ├── sweeper.py             # Reaction sweeper + ChatOps command parser
│   ├── tailor.py              # Cloud LLM escalation engine
│   ├── compile.py             # Document compiler (.docx generation)
│   ├── research.py            # Deep Research agent (semantic scraping)
│   ├── report.py              # CLI funnel report (pre-flight + metrics)
│   ├── batch_process.py       # Batch processor — concurrent OpenRouter tailor requests
│   ├── email_fetcher.py       # IMAP connection and retrieval
│   ├── email_classifier.py    # Header-based email classification
│   ├── text_extractor.py      # Generic HTML → text extraction
│   ├── triage.py              # LLM extraction + matching (confidence threshold)
│   ├── geo.py                 # Geo-distance calculator (Nominatim + haversine)
│   ├── models.py              # JobListing dataclass
│   ├── profile_loader.py      # Profile.md loader
│   ├── notifications.py       # Slack integration (optional)
│   ├── proxy_manager.py       # IPRoyal sticky residential-proxy rotator
│   ├── proxy_test.py          # CLI smoke test for the IPRoyal stack
│   └── db.py                  # SQLite schema and access layer
├── eval/
│   ├── eval.py                # Eval harness
│   └── eval_example.csv       # Sample eval data
├── tests/
├── docs/
│   ├── PROJECT_BRIEFING.md
│   └── EVAL_GUIDE.md
├── SECURITY.md
├── .env.example
├── .gitignore
└── pyproject.toml
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

- [ ] **The Command Center GUI (Next.js)** — A lightweight local web dashboard that connects to the SQLite DB to visualize the full application funnel (ingested → triaged → saved → tailored → applied). Provides an interface to review and curate the `human_labels.jsonl` dataset for future model fine-tuning. Triage stays in Slack; management and analytics move to this GUI.
- [ ] **The Dynamic RAG "Brag Document"** — Shift from editing a single `base_resume.docx` to dynamic assembly. A massive `master_brag_document.md` stores every bullet, project, and achievement. The pipeline semantically searches this document against the job description, pulling only the top most relevant bullets for the LLM. Eliminates hallucinations and produces hyper-targeted resumes.
- [ ] **The "Warm Intro" API (Cold Outreach Copilot)** — Repurpose cold outreach into a bridge feature. Uses Deep Research context to autonomously draft a highly targeted, 3-sentence DM. Exposed via a central API endpoint so it can be routed to the user's Slack for manual LinkedIn messaging, or eventually piped into a partner ATS/recruiter dashboard.

### Future / Icebox

- [ ] **"Hosted Receipt" Verification** — Generate public, read-only web links of `deep_research_context.txt` to prove the application was AI-researched with real company data.
- [ ] **Interactive Mock Interview Agent** — A Slack command (`/interview`) triggering an agent to act as the hiring manager in a threaded conversation, testing technical fit before the real interview.
- [ ] **Headless Auto-Apply via `browser-use`** — Navigate ATS portals autonomously to submit applications. Currently iceboxed due to brittleness from constant DOM changes across ATS platforms.
