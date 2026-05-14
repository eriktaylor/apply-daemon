# Changelog

All notable shipped features for Apply Daemon. For in-flight and upcoming work, see the **Roadmap** section in [`README.md`](README.md).

## Shipped

- **Confidence-threshold scoring (replaces ensemble)** — Single-model Stage 5 scoring with a configurable `CONFIDENCE_THRESHOLD` (0.0–1.0). Listings scored below the threshold are auto-rejected; the same value also raises the AUTO_MATCH cutoff when set above 0.8. The legacy `OPENROUTER_ENSEMBLE_MODELS` / `JD_REJECTION_MODE` env vars are removed and now log a one-time deprecation warning at startup if still set. Migration: set `CONFIDENCE_THRESHOLD=0.5` for `hard_no`-equivalent behavior or `0.0` for `accept_all`.
- Scheduled email fetching via cron
- Fuzzy deduplication with `rapidfuzz`
- Historical context timeline for reposted listings
- Slack rate-limit retry handlers and pacing
- Hard No ensemble filter (multi-model unanimous vote)
- Geo-distance calculator with dynamic home location
- Batch processing edge-case protections (TTL, stuck reversion)
- Skills match matrix in digest (job-required skills with gap analysis)
- Recruiter outreach scoring floor (auto-upgrade to MAYBE)
- Deep Research Agent — OpenRouter query generation + trafilatura scraping grounds tailoring in actual company data
- Diff-based CV & Cover Letter Editing — Resume bullet edits shown as Slack diffs for instant human review
- Cover letter style reference — LLM matches your writing tone from a sample cover letter
- Job links in digest — Clickable title links to the original job posting
- Per-model ensemble breakdown in digest
- Two-step Deep Evaluation — Research dossier + 3-part match analysis with re-scored verdict, posted as threaded Slack reply
- Human feedback ledger — JSONL records (`data/human_labels.jsonl`) capturing every reaction for DPO fine-tuning
- Training data dump — `original_triage.json`, `tailored_analysis.json`, `deep_research_context.txt` saved per job for model training
- Threaded Slack UX — Original triage message preserved; Deep Evaluation posted as thread reply for comparison
- Custom Application Questions — Unified Intake (thread replies + Tailor in one pass) and Late Intake (❓ reaction fast-path for already-tailored jobs)
- `batch_process_days` setting — Limit batch submissions to recently saved jobs only
- Indeed email classification — Support for `jobalert.indeed.com` sender detection
- ChatOps state tracking — `!applied`, `!pass`, `!interview`, `!rejected` thread commands with status badge UI
- CLI funnel report (`src/report.py`) — Dual-query metrics with conversion rates and pre-flight check
- Token-optimized default pipeline — Resume-only default run; cover letter, interview prep, and polished resume moved to on-demand `!coverletter`, `!prep`, and `!polish` ChatOps commands
- Smart Router (`❓` / `!answer`) — Context-aware dispatch: fallback to tailor, Route A (full pipeline + answers), or Route B (fast-path cached answers)
- Dual-track ingestion — Track A (JobSpy proactive polling via `my_profile/search_config.yaml`) + Track B (reactive email/Slack pipeline) with Smart Upsert dedup across both tracks. Eliminates dependency on job boards sending email alerts — scrapes Indeed, LinkedIn, Glassdoor, and Google directly.
- Site tiers × searches config matrix — `my_profile/search_config.yaml` separates boards by scraping reliability (`friendly` / `ok` / `hostile`) from search terms and locations. Every search runs against every enabled tier; disable a tier with `results_wanted: 0`.
- Stage 4b lazy loading — Track A automatically fetches the full job description from the ATS or Indeed detail page when the scraped preview is under 300 words or ends with a truncation marker. LinkedIn full descriptions fetched at scrape time via `linkedin_fetch_description=True`.
- Google Jobs source board extraction — Google Jobs email digests include a "via [Board]" line. Stage 1 extracts the board domain and Stage 3 uses `site:<domain>` in the DDGS query for higher-quality URL discovery.
- Speculative synthesis fallback (infallible) — When all URL scrapes and DDGS healing fail, the pipeline now always produces a job description by running three escalating DDGS passes (company → company + title → company + title + location) and synthesising a clearly-prefixed speculative JD from the snippets. Falls back to the LLM's priors when DDGS returns nothing. Escalation to Slack removed — no listing is ever silently dropped.
- Tracking URL detection and canonical URL replacement — Google Alert notification URLs (`notifications.googleapis.com`, `google.com/url?`) are detected and discarded. The replacement URL is the top DDGS result href, or a DuckDuckGo safe-search URL (`duckduckgo.com/?q=Company+Title+Location`) when DDGS returns nothing. Dead URLs never reach the database.
- Stage 3 anchor hallucination prevention — Digest emails with no scrapable URL now trigger DDGS heal (not source-text fallback). Hallucinated anchors fail DDGS and are dropped cleanly; real listings with tracker URLs get healed.
- Skills aggregation fix for Hard No ensemble — Matching and missing skills are now correctly unioned across all ensemble model evaluations. Previously hard-no paths returned 0/0 skills.
- OpenRouter integration — Unified LLM routing via the OpenAI-compatible SDK replaces local Ollama and direct Anthropic API calls. All triage, tailoring, deep research, and batch processing route through a single `OPENROUTER_API_KEY`. Batch processing migrated from Anthropic's async Batch API to concurrent `asyncio.gather` over OpenRouter — completes synchronously within the cron window.
- `!trend` labor market intelligence — Post `!trend` in Slack to get a skill frequency report across the last 100 scored jobs, split into High Intent / Pipeline / Rejected cohorts. A lightweight LLM pass canonicalizes synonym clusters (`GenAI` / `Generative AI` / `LLMs` → one entry) via 3 concurrent async OpenRouter calls before ranking. Output is a two-column monospace code block showing top-10 matched and missing skills per cohort.
