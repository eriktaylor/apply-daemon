# apply-daemon — project briefing

## Project overview

A local-first job search automation pipeline that monitors job alert emails, triages listings against a candidate profile using a local LLM, and surfaces the best matches. The system uses an LLM-first architecture: after email classification, a single LLM call per email handles extraction, field parsing, matching, and scoring.

## Architecture

```
IMAP fetch → Email classifier → Text extraction → Dedup → LLM triage → SQLite + Slack
```

1. **Email ingestion**: Job alerts from LinkedIn, Indeed, Google Alerts, etc. are delivered to a dedicated Gmail inbox. IMAP fetch pulls unread messages.

2. **Email classification** (zero cost): Regex on headers classifies emails as JOB_DIGEST / RECRUITER_OUTREACH / GOOGLE_ALERT / SKIP. Prevents sending irrelevant emails to the LLM.

3. **Text extraction** (zero cost): Generic, template-agnostic HTML-to-text conversion via BeautifulSoup. Works on any email from any platform — no platform-specific parsers. When LinkedIn changes their template, nothing breaks.

4. **Dedup** (zero cost): Text similarity dedup at the email level (before LLM call) using SequenceMatcher on first 500 chars. Title+company dedup at the listing level (after LLM) against the database.

5. **LLM triage** (~800ms per email): One LLM call per email extracts all job listings AND scores them against the candidate profile. The LLM reads the raw email text and the natural-language profile, then outputs structured listing data with verdicts (YES/NO/MAYBE) and reasoning.

6. **Storage + notification**: Results saved to SQLite. Optional Slack notifications with action buttons (Save/Pass/Tailor).

## Hardware and infrastructure

- **Local machine**: NVIDIA GPU with 6 GB dedicated VRAM. The 6 GB VRAM comfortably fits a 4B model fully on GPU via ollama.
- **Email**: Dedicated Gmail account with App Password for IMAP access.
- **Storage**: SQLite database (WAL mode) for listings and processed email tracking.
- **Default model**: `gemma3:4b` — ~800ms per email, ~3 GB VRAM.

## Key files

- `my_profile/profile.md` — Natural-language candidate profile. Everything before "Pipeline Settings" is sent to the LLM as context. Written like a candidate brief — who you are, what you want, what you don't want.
- `src/pipeline.py` — Main orchestrator (fetch → classify → extract → dedup → LLM → store).
- `src/triage.py` — Unified LLM extraction + matching. One call per email, structured output.
- `src/text_extractor.py` — Generic HTML → text. Template-agnostic.
- `src/models.py` — `JobListing` dataclass.
- `src/db.py` — SQLite schema and access layer.

## Data model

```
listings
├── id                  TEXT PRIMARY KEY (uuid)
├── source              TEXT (linkedin | google_alerts | recruiter | unknown)
├── email_classification TEXT (JOB_DIGEST | RECRUITER_OUTREACH | GOOGLE_ALERT)
├── title               TEXT
├── company             TEXT
├── location            TEXT
├── salary              TEXT (free text — "$220K-$485K" or "not listed")
├── verdict             TEXT (YES | NO | MAYBE)
├── reason              TEXT (one-sentence LLM explanation)
├── links               TEXT (JSON array of URLs)
├── recruiter_name      TEXT (nullable)
├── recruiter_title     TEXT (nullable)
├── raw_email_text      TEXT (full extracted text for debugging)
├── model_used          TEXT (e.g. "gemma3:4b")
├── tokens_used         INTEGER
├── latency_ms          INTEGER
├── date_ingested       TEXT (ISO datetime)
├── final_status        TEXT (new | reviewed | applied | archived)
└── updated_at          TEXT

processed_emails
├── id                  INTEGER PRIMARY KEY
├── email_text_hash     TEXT (SHA256 of first 500 chars)
├── text_preview        TEXT (first 500 chars for similarity dedup)
└── date_processed      TEXT
```

## Design principles

1. **The LLM is the pipeline.** After email classification, the LLM does everything: extraction, field parsing, matching, scoring. No deterministic pre-processing of job content.

2. **Text in, structured data out.** Every email goes through one transformation: HTML → plain text. Then the LLM reads the plain text and outputs structured listing data.

3. **Template-agnostic.** The system never depends on specific HTML structure from any platform. LinkedIn can change their email template tomorrow and nothing breaks.

4. **Profile as prose.** The candidate profile is natural language, not tables of keywords and weights. The LLM reads it like a human recruiter would read a candidate brief.

5. **Fail gracefully.** If the LLM returns malformed output, log it and move on. Never crash the pipeline.

6. **Observable.** Store the raw email text, the LLM's structured output, and the parsed listings. If triage quality degrades, inspect exactly what the model saw and what it said.

## Eval framework

The eval harness tests the full extraction + matching pipeline:

- **Input**: Raw email text + expected listings (title, company, verdict)
- **Metrics**: Extraction accuracy (did it find the listings?), verdict accuracy (did it score them right?), tokens, latency
- **Usage**: `python -m eval.eval --input eval/eval_example.csv --model gemma3:4b`
