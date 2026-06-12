# Profile pipeline — outline & upgrade ideas

How `my_profile/` (and specifically `profile.md` + `base_resume.{docx,md,pdf}`) flows
through the apply-daemon pipeline today, followed by a ranked shortlist of upgrades.

---

## 1. How `my_profile/` is used

**Layout.** Convention-over-config. `my_profile/` is a gitignored dropzone:

- `profile.md` — required, free-form markdown.
- `base_resume.{docx,md,pdf}` — required for tailoring (`.docx` wins, then `.md`, then `.pdf`).
- `cover_letter.{docx,md,pdf}` — optional style reference for cover-letter assets.
- `search_config.yaml` — Track A (JobSpy) search terms.

**Two readers.**

- `src/profile_loader.py::load_profile()` parses `profile.md`. It splits on
  `## Pipeline Settings`: everything above becomes `llm_context` (sent
  verbatim to LLMs); the table below is parsed into a typed `settings` dict
  (`max_listings_per_run`, `dedup_window_days`, `pass_window_days`,
  `generate_assets`, etc.). `## Job Alert Configuration` is stripped — it's
  reference-only.
- `src/file_utils.py::read_dropzone_file(base)` resolves
  `my_profile/<base>.{docx,md,pdf}` (priority order) and extracts text —
  `.docx` via python-docx (paragraph join), `.md` raw, `.pdf` via pdfplumber.

**Where it's injected.**

| Stage | Module | Model env var | What it sees |
|---|---|---|---|
| Stage 1 — email listing extraction | `src/triage.py` (`_SINGLE_PROMPT`) | `OPENROUTER_STAGE1_MODEL` (default `openai/gpt-5.4-nano`) | `profile.llm_context` + raw email text → LISTING blocks (title/company/skills/verdict). |
| Stage 5 — match scoring | `src/triage.py` (`_EVALUATE_PROMPT`) | `OPENROUTER_MODEL` (default `google/gemini-3.1-flash-lite`) | `profile.llm_context` + structured listing → YES/NO/MAYBE + confidence + matching/missing_skills + job_summary. No resume yet. |
| Autopilot — post-research re-score | `src/process_queue.py` (`_AUTO_PROMPT`) | `OPENROUTER_TAILOR_MODEL` (default `anthropic/claude-sonnet-4.6`) | `profile.llm_context` + `base_resume` + listing fields + deep-research dossier → `match_analysis`, `post_research_verdict/confidence`, `updated_skills_match`. |
| Tailor — resume edits | `src/tailor.py` (`_TAILOR_PROMPT`) | `OPENROUTER_TAILOR_MODEL` | `profile.llm_context` + `base_resume` + optional `cover_letter` style reference + research + listing → resume bullet edits, executive summary rewrite, optional cover letter / interview prep. |
| On-demand (`!coverletter`, `!prep`, `!polish`) | `src/tailor.py` | `OPENROUTER_TAILOR_MODEL` | Same profile + resume, plus cached research + cached tailor analysis. |

**Prompt shape (all stages share the pattern):**

1. Role line ("You are a recruiting assistant…" / "You are an expert career coach…").
2. `## Candidate profile` ← `profile.llm_context` (your free-form markdown verbatim).
3. `## Base Resume` ← `base_resume.{docx,md,pdf}` text (tailor + autopilot only —
   Stage 1/5 omit it to keep cost low).
4. `## Target Job` block — title, company, location, salary, summary/description.
5. Optional `## Company Research` block — deep-research markdown when present.
6. `## Instructions` — directs JSON output with a strict schema;
   `response_format={"type": "json_object"}` is set on the tailor/autopilot
   OpenRouter calls.

**Settings table.** Parsed from `## Pipeline Settings` and consumed by
`pipeline.py`, `jobspy_ingest.py`, `batch_process.py`, `tailor.py`. Notably
`generate_assets` (resume / cover_letter / interview_prep) gates which prompt
fragments `build_prompt` includes in the tailor schema — env `GENERATE_ASSETS`
overrides the profile setting.

**Name extraction.** `_extract_name` pulls a candidate name from the first
content line under `## Who I am` (or falls back to the `# Heading`), used for
display/logging only.

**Gating before any LLM call.** `src/integration_test.py` checks that both
`profile.md` and `base_resume.{docx,md,pdf}` exist before reporting Track A/B
as ready.

Net effect: the same free-form `llm_context` is the single source of truth for
"who is this person" across every stage; `base_resume` is only loaded into the
heavier tailor/autopilot prompts where it actually pays for itself.

---

## 2. Upgrade ideas — ranked by Impact ÷ Difficulty

Scoring is 1–5 (higher = better). ROI = Impact ÷ Difficulty.

| Rank | Idea | Diff | Impact | ROI |
|---|---|---|---|---|
| 1 | **Prompt caching on the profile + resume preamble** (Anthropic `cache_control` via OpenRouter). The same `llm_context` + `base_resume` text is re-sent on every Stage 5, autopilot, tailor, and batch call. With autopilot running 10–20 listings concurrently, caching the static prefix is a big cost cut. | 2 | 5 | 2.5 |
| 2 | **`python -m src.profile_loader --check` lint.** Today, missing `## Pipeline Settings` or `## Who I am` silently degrades — name becomes `""`, settings fall back to defaults. A one-shot lint catches mistakes before a batch burns credits. | 1 | 2 | 2.0 |
| 2 | **Tailor-compile preflight warning.** `compile.py` clones `base_resume.docx` specifically; if the user only has `.md` or `.pdf` in the dropzone, tailoring runs the LLM call then fails at compile. Surface that as a startup error in `integration_test.py`, not a runtime crash. | 1 | 2 | 2.0 |
| 4 | **Two-tier `llm_context`.** Stage 1 (extraction) doesn't need "What I'm looking for / What excites me" — it only needs a slim preferences digest to disambiguate listings. Splitting into `llm_context_slim` (Stage 1) and `llm_context_full` (Stage 5 + tailor + autopilot) saves tokens on every email processed. | 2 | 3 | 1.5 |
| 5 | **Structured skills section.** Parse Expert / Proficient / Familiar into typed JSON in `profile_loader`, then inject as a clean block. Easier for the model to compare against `missing_skills` in `_EVALUATE_PROMPT` and reduces hallucinated "matching" claims. | 3 | 3 | 1.0 |

**Top pick** is **(1) prompt caching** — it compounds best as autopilot scales,
and the implementation surface is just two `cache_control` breakpoints in
`_AUTO_PROMPT` and `_TAILOR_PROMPT`.
