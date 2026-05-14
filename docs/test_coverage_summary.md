# Apply-Daemon Test Coverage Summary

**Generated:** 2026-04-24
**Total tests:** 421 passing (0 failures, 0 collection errors)
**Test files:** 17 unit/integration suites + 1 eval harness

---

## 1. Coverage by Module

### `src/triage.py` — `tests/test_triage.py` (~80 tests)

Core LLM pipeline. All calls now route through a single `openai.OpenAI` client
pointed at `https://openrouter.ai/api/v1`. The `_call_openrouter` helper replaces
the former `_call_ollama`; `<think>`-tag stripping and Ollama-specific timeouts are
gone. Stage 5 evaluation now receives up to 4,000 chars of JD text (doubled from
2,000). Skills extraction prompt broadened to capture domain expertise and
professional competencies, not just hard engineering tools.

| Area | Tests | Behavior covered |
|---|---|---|
| `TestParseBlockFields` | 3 | Key/value block parsing for standard + recruiter fields; empty-block tolerance |
| `TestParseTriageResponse` | 11 | Single/multi-listing parsing; invalid verdict → MAYBE; garbage text → empty; link fallback to email links; source/classification propagation; default confidence (50); model score aggregation; single-model default status; job-summary parsing |
| `TestParseExtractionResponse` | 5 | Multi-listing extraction; recruiter fields; link fallback; empty response; job_summary extraction |
| `TestParseEvaluationJson` | 6 | Valid JSON; markdown-fenced JSON; verdict normalization; confidence clamping (low/neg); garbage + empty fallback |
| `TestCompositeVerdict` | 6 | Unanimous YES/NO; YES+MAYBE; YES+NO conflict; MAYBE+NO; empty list |
| `TestConsensusLabel` | 7 | Auto_match vs needs_review thresholds; single-model path; unanimous NO/MAYBE → standard |
| `TestSkillsMatrix` | 9 | Skills JSON extraction; missing/invalid field defaults; fallback path; single-model skills parse |
| `TestRecruiterOverride` | 6 | Recruiter NO → MAYBE upgrade; YES preserved; MAYBE unchanged; DIGEST NO stays NO; save-as-triaged path; model_scores reflection |
| `TestHardNoConsensus` | 9 | Unanimous NO verdict + `was_auto_rejected=True` flag (case-insensitive); single dissent blocks rejection; edge cases (empty, all-YES, mixed, single-model). **Updated:** assertions changed from `"auto_rejected"` to `"NO"` to match production change. |
| `TestGetModelTemperature` | 2 | `is_repeated=False` → 0.0; `is_repeated=True` → 0.7. Signature-confirmed against production function — not stale. |
| `TestEvaluateScrapeValidity` | 4 | Judge accepts real job descriptions; rejects Cloudflare/block pages; unparseable → invalid; partial-extraction wrappers |
| `TestIsAggregatorUrl` | 6 | Blocks Indeed/Glassdoor/LinkedIn + subdomains; allows direct ATS / company career pages |
| `TestValidateAndHealIntegration` | 11 | **Updated (was 7 failures):** 2 tests fixed to mock the batch judge `_call_openrouter` path (DDGS heal was refactored from per-URL `evaluate_scrape_validity` to a single batch LLM call); 5 tests renamed from `*_synthesis_*` to `*_listing_dropped` to reflect that `_ddgs_heal` drops listings when all scrapes fail (no synthesis fallback is wired). Recruiter/Stage-2 hard-stop paths unchanged. |

### `src/db.py` — `tests/test_db.py` (~50 tests)

SQLite persistence layer.

- **Insert/query:** basic insert, duplicate-ID ignore, query by verdict
- **Listing dedup:** exact + fuzzy (case, minor variation, token reorder, threshold, None handling)
- **Upstream dedup:** `is_duplicate_listing()` is called before Stage 5 in both Track A (JobSpy) and Track B (email). The method itself is tested via the dedup suite above, but the **pre-Stage-5 short-circuit behavior** in `jobspy_ingest.py` and `triage.py` has no dedicated integration test.
- **Email dedup:** duplicate detection, threshold sensitivity, empty state
- **Processed-email ledger**, **pipeline status transitions** (incl. invalid-status rejection, nonexistent listing)
- **`get_listing_by_id`**, **`get_digest_listings`** (ordering, exclusion of passed/tailored states)
- **Batch processing:** `set_batch_id`, retrieval by batch, processing-batch-id tracking, completed-batch exclusion, saved-listing ordering
- **Slack notified flag:** mark/exclude logic; unanimous-NO auto-dismissed listings now start as `pipeline_status="rejected"` so the status gate (`IN ('triaged', 'saved')`) excludes them — the former `verdict != 'auto_rejected'` SQL filter has been removed
- **Listing history:** no-history empty; single + multiple prior encounters; fuzzy matching; triaged→ignored mapping; current-job-id exclusion; different-company isolation; chronological order
- **`format_history_timeline`:** 1/4/5/6-entry truncation rules (first + last 3)
- **`get_trend_skills`** *(new — untested):* returns last N rows with skills data for `!trend` cohort analysis. No test verifies the SQL filter, LIMIT, or ordering.

### `src/research.py` — `tests/test_research.py` (~25 tests)

Deep-research subsystem. LLM synthesis now calls `openai.OpenAI` → OpenRouter
(replacing `_openrouter_generate`, which itself replaced `_ollama_generate`).
Embedding is computed locally via cosine similarity over TF-IDF-style chunking —
no Ollama embedding endpoint.

- **Graceful degradation:** timeout, connection error, generic exception → empty result
- **Bypass logic** when research disabled (via tailor)
- **Chunking:** empty, single-word, single-chunk, exact boundary, massive repetitive input
- **Cosine similarity:** identical, orthogonal, zero, empty, mismatched lengths
- **DuckDuckGo search & scrape:** URL list return, exception → empty, scrape-fail empty
- **Search pacing:** sleep between queries, no sleep for single query
- **Embedding filter:** relevant chunk ranked top, irrelevant filtered when top_k=1
- **Synthesis pipeline:** contains key terms; empty when no scrape
- **Profile settings:** `enable_deep_research` bool parse; `generate_assets` list parse

### `src/digest.py` — `tests/test_digest.py` (~25 tests)

Slack digest rendering. No changes to digest logic in this sprint.

- Header blocks (stats, auto_match/escalate counts)
- Listing attachments: green/yellow/blue color logic by verdict+confidence
- Reaction legend; no action buttons (read-only digest)
- Job summary shown/hidden; saved-status icon
- Skills matrix rendering: N/A, 100%, partial, only-missing, DB integer flag
- Link vs plain header
- Ensemble model scores displayed
- History context block: none / single / multiple; ordering relative to reaction legend
- **Geo distance:** with distance, remote, unknown, no location
- **Post pacing:** sleep after each listing; rate-limit handler attachment

### `src/compile.py` — `tests/test_compile.py` (~30 tests)

Asset compilation (resume/cover letter docx). No changes this sprint.

- `generate_assets`: creates output dir, cover letter docx, match analysis MD, assets JSON; slugged dir name; edits-only doc path (no baseline) vs targeted resume (with baseline)
- `generate_cover_letter`: valid docx; double-newline paragraph split
- `find_resume_baseline`: finds docx, returns None, prefers most recent
- `extract_clean_bullets`: structured edits, plain-string fallback, mixed formats, missing clean-bullet skip, empty list
- **Compiler safety:** aggressive markdown excluded from docx; clean cover letter used; backward-compat `custom_cover_letter`; interview prep saved
- **Missing-diff failsafe:** `_format_diff_text` variants (missing Slack diff, no edits, valid diffs, plain string edits)
- **`TestExecutiveSummaryAndSuggestions`:** `executive_summary_rewrite` saved as MD; `other_suggestions` saved as MD; missing fields do not create files; heading-match heuristic replaces correct paragraph; fallback heuristic (first long paragraph); no-match leaves doc unchanged
- **`TestCompilerSafety`:** `clean_cover_letter_text` used in docx over diff summary; backward-compat `custom_cover_letter` fallback; `interview_prep_guide` saved

### `src/batch_process.py` — `tests/test_batch_process.py` + `tests/test_batch_edge_cases.py` (~12 tests)

Batch processor migrated from Anthropic's async Batch API to concurrent
`asyncio.gather` over OpenRouter. Queue management logic (TTL, stuck reversion,
partial failures) is unchanged and still covered.

- **Happy path:** no pending/no saved; retrieves pending; retrieve-still-processing; submits saved; retrieve-error counted
- **Queue rot TTL:** stale saved listing expires, fresh one batched; batch_process expires before submitting
- **Partial batch failures:** error result on one marked failed while others succeed
- **Compilation failure isolation:** one failure doesn't poison siblings
- **Stuck batch reversion:** stuck batch reverted to saved; recent batch NOT reverted; revert runs before retrieve

### `src/email_classifier.py` — `tests/test_email_classifier.py` (~15 tests)

No changes this sprint.

- Google Alerts (sender + any subject)
- LinkedIn job alerts (`job-alert`/`jobalerts` variants), Indeed, Glassdoor senders
- Subject patterns ("job alert", "new jobs")
- Recruiter outreach: LinkedIn InMail + notification + corporate with outreach subject; LinkedIn notification body match
- Skip rules: known skip sender; LinkedIn social (viewed profile, connection request, endorsed, birthday)
- Defaults: unclassified → skip; gmail sender w/o outreach subject

### `src/sweeper.py` — `tests/test_sweeper.py` + `tests/test_idempotency.py` (~61 tests)

**Still undertested relative to module size, but idempotency, eligibility gating, and key state contracts are now covered.**

**Covered in `test_sweeper.py`:**
- `_extract_job_id`: valid metadata; wrong event type; no metadata; missing job_id
- `_get_user_reactions`: returns reactions; filters `white_check_mark`/`eyes` bot receipts; empty; no reactions key
- `_classify_reaction`: thumbsdown → pass, thumbsup → save, pencil → tailor, unknown → None
- `_extract_triage_url`: Slack angle-bracket unwrapping, trailing-text stripping, http/https, None fallback
- `_classify_trend_cohort`: full status × verdict matrix (high_intent, pipeline, rejected cohorts); `"NO"` verdict from unanimous rejection routes correctly to `"rejected"` (gap §3.2 #1 **closed**)
- `TestChatOpsEligibilityGate`: `!coverletter`/`!prep` no-op on ineligible statuses (triaged/passed/rejected); fire correctly on eligible statuses (saved/tailored/applied)
- `TestHandleUpdateStatusContract`: `_handle_update` resets `pipeline_status` to `"triaged"` after successful re-score, even from a terminal state like `"rejected"` (gap §3.1 #3 **closed**)

**Covered in `test_idempotency.py`:**
- **`TestChatOpsIdempotency`:** `!coverletter` and `!prep` honor the `_reply_is_processed` guard; first sweep processes, second sweep skips
- **`TestJITStatusReset`:** `_handle_triage_jit` with Smart Upsert overwrite resets `pipeline_status` to `"triaged"`
- **`TestSmartUpsertStatusPreservation`:** on fuzzy match, `upsert_listing` preserves `pipeline_status` and `slack_notified`; scoring fields still overwritten
- **`TestReplyProcessedMarker`:** `_reply_is_processed` / `_mark_reply_done` round-trip; idempotent against `already_reacted`
- **`TestTruncatedLLMResponse`:** `_strip_code_fence` handles unterminated code fences

**Still not covered:**
- `_handle_update` merge logic (`raw_email_text` + `--- ADDITIONAL MANUAL CONTEXT ---` delimiter); `chat.update` in-place card edit
- `_handle_pass` fallback thread reply (new code in `except` block — visible behavior, untested)
- `_handle_smart_router` dispatch between `_handle_tailor` and `_handle_answers_fast`
- `_handle_answers_fast` cached-research fast-path
- `_scan_triage_commands` / `_scan_triage_fallback_commands`: `already_handled` broad-bot-reply false positive (§3.1 #1–2)
- `_format_trend_section` / `_format_trend_report`: monospace column layout, top-10 truncation, empty-cohort fallback
- `_scan_trend_commands`, `_handle_trend`: `!trend` dispatcher and full-report rendering
- `_handle_tailor`: end-to-end ChatOps → tailor → compile → post wiring
- `_fetch_thread_questions` ChatOps filter

### `src/notifications.py` — `tests/test_notifications.py` (~12 tests)

No changes this sprint.

- **Listing attachment builder:** auto_match color, escalate color, model scores rendered, job summary shown/hidden, action buttons present
- **Header blocks:** stats and auto_match/escalate counts
- **Slack error handling:** `not_in_channel`, `channel_not_found`, `invalid_auth` → logged warnings

### `src/geo.py` — `tests/test_geo.py` (~11 tests)

No changes.

- `haversine`: same point, known distance, long distance
- `get_distance`: remote bypass, empty string, known location, geocode failure, no home coords
- `init_home`: success, geocode-failure defaults, missing home_location

### `src/file_utils.py` — `tests/test_file_utils.py` (~8 tests)

No changes.

- `read_dropzone_file`: no file returns None; `.md` only; `.docx` preferred over `.md`; `.md` preferred over `.pdf`; `.pdf` fallback; cover-letter filename
- Docx reader extracts paragraphs; PDF reader extracts text from pages

### `src/tailor.py` — `tests/test_tailor.py` + `tests/test_idempotency.py` (~14 tests)

- `_parse_tailor_response`: valid JSON; new diff schema (`clean_cover_letter_text`, `cover_letter_diff_summary`, nested `slack_diff`/`clean_bullet`); markdown fences; missing `match_analysis` raises; `resume_bullet_edits` not a list raises; invalid JSON raises
- `test_new_resume_fields_parse`: `executive_summary_rewrite` and `other_suggestions` pass through cleanly
- `_save_assets`: output files created; assets JSON content; directory name slugged
- **`_strip_code_fence` *(new, covered in `test_idempotency.py`)*:** handles truncated responses (opening fence only), naked JSON (no fence), valid fenced JSON (``` or ```json), and empty payloads

### `src/profile_loader.py` — `tests/test_profile_loader.py` (~11 tests)

No changes.

- Name extraction
- `llm_context`: non-empty, contains who_i_am / skills / what_looking_for; **excludes** pipeline_settings and job_alert_config (token hygiene)
- Settings parsed; home location parsed; ensemble mode parsed
- Missing profile raises

### `src/text_extractor.py` — `tests/test_text_extractor.py` (7 tests)

No changes.

- `extract_text`: removes scripts/styles/hidden elements; preserves content; collapses blank lines
- `extract_links`: filters tracking URLs; skips non-http

---

## 2. Eval Harness — `eval/eval.py`

Not a unit test suite — a benchmark runner for the LLM pipeline, driven by CSV input.

**Metrics computed:**
- Extraction accuracy (title-match rate)
- Verdict accuracy (for matched listings)
- JSON parse success rate (fallback verdicts flagged)
- Avg latency + tokens per email
- Throughput (tok/s) — **⚠ vestigial for cloud APIs;** meaningful for local models only, now reflects network + server-side generation speed
- **False Positive Rate** (model YES when expected NO/SKIP)
- **False Negative Rate** (model NO when expected YES)

**Scope:** end-to-end `TriageSession.triage_email` invoked per row. Supports
`--model` override for any OpenRouter slug, `--runs` for repeated sampling.
Model defaults to `OPENROUTER_MODEL` env var. Reports to stdout + CSV.

**Removed / no longer applicable:**
- Cold-start latency probe (`OLLAMA_NUM_CTX`, unload → dummy prompt) — removed from runtime; cloud APIs have no warm-up concept
- `_ollama_embed` / `_call_ollama` transport assertions — transport is now the OpenAI SDK

---

## 3. Gap Analysis

Gaps are organized around four directives surfaced by recent live-log triage:
**(1) Idempotency Failures**, **(2) State Machine Transitions**, **(3) External API
Error Handling**, and **(4) Combined Operations**. The 2026-04-18 summary bucketed
gaps by module — that view is preserved in §3.5 / §3.6 as a carry-forward.

### 3.0 What the 2026-04-24 sprints added

**Sprint 1 — `tests/test_idempotency.py` (23 tests):** ChatOps idempotency (`!coverletter`/`!prep`), JIT triage status reset, Smart Upsert field-preservation contract, `white_check_mark` reply-processed marker, truncated LLM response handling.

**Sprint 2 — `tests/test_sweeper.py` extensions (26 tests):** `_extract_triage_url`, `_classify_trend_cohort` full matrix, `TestChatOpsEligibilityGate`, `TestHandleUpdateStatusContract`.

**Sprint 3 — Bug fixes + CI cleanup (0 net new tests, 7 failures → 0):**
- `auto_rejected` verdict replaced with `"NO"` throughout; unanimous machine rejections now set `final_status="rejected"` (suppressed from Slack queue via status gate)
- `_handle_update` now resets `pipeline_status` to `"triaged"` after successful re-score
- `_handle_pass` now posts a thread reply fallback when `chat_update` fails
- `TestValidateAndHealIntegration` updated to mock batch judge path; 5 synthesis-path tests renamed to reflect current "drop on failure" behavior
- `pyproject.toml` dev deps: `pandas` and `beautifulsoup4` added explicitly → collection errors for `test_jobspy_ingest.py` and `test_text_extractor.py` resolved

Everything below is **remaining** gaps.

### 3.1 Idempotency Failures — remaining gaps

The central invariant is: *"every ChatOps command processes at most once per
reply, regardless of how many sweep cycles run."* The new suite covers the
reaction-marker round-trip. These paths still violate or neglect the invariant:

| # | Risk | Location | Gap |
|---|---|---|---|
| 1 | **High** | `sweeper.py::_scan_triage_commands` (~line 1134) | `already_handled = any(r.get("bot_id") for r in replies if r.get("ts") != ts)` — *any* bot reply (even an unrelated "⚠ Scrape failed" warning) suppresses re-dispatch. No test covers a mixed-bot-reply thread that should still process `!triage`. |
| 2 | **High** | `sweeper.py::_scan_triage_fallback_commands` | Same broad-bot-reply guard; a JIT fallback `!update` after a failed scrape has no idempotency test for the case where the user edits-and-resends their paste. |
| ~~3~~ | ~~Medium~~ | ~~`sweeper.py::_handle_update`~~ | **CLOSED.** `_handle_update` now calls `db.update_pipeline_status(..., "triaged")` after a successful re-score. `TestHandleUpdateStatusContract` verifies this. |
| 4 | **Medium** | `sweeper.py::_handle_smart_router` | Routing between `_handle_tailor` and `_handle_answers_fast` is based on live state; no test verifies that a second ❓ reaction (after the first has already been marked ✅) is skipped. |
| 5 | **Medium** | `batch_process.py::submit_batches` | Stuck-batch reversion + concurrent sweeper pickup: no test for the race where a listing is reverted to `saved` while another process is mid-submit. |
| 6 | **Medium** | `pipeline.py::main` | End-to-end re-run on the same unread email set: `mark_email_processed` is the ledger guard, but there's no integration test verifying zero side-effects on a replay. |
| 7 | **Low** | `db.py::mark_slack_notified` | Idempotent on repeat, but no test for the read-modify-write race (two sweeper workers setting the flag concurrently in WAL mode). |
| 8 | **Low** | `sweeper.py::_append_human_label` | Appends to `human_feedback`; no test for repeated appends producing duplicate labels. |

### 3.2 State Machine Transitions — remaining gaps

`Database.VALID_STATUSES` is the whitelist. `db.update_pipeline_status` is tested
for whitelist enforcement. These transition paths are unverified:

| # | Risk | Location | Gap |
|---|---|---|---|
| ~~1~~ | ~~High~~ | ~~`triage.py` + `sweeper.py::_classify_trend_cohort`~~ | **CLOSED.** `auto_rejected` verdict eliminated — `_hard_no_consensus` now returns `"NO"`. `_classify_trend_cohort` already routes `verdict == "NO"` to `"rejected"` cohort. `TestClassifyTrendCohort` verifies the full matrix. |
| 2 | **High** | `sweeper.py` | Whitelist `_CHATOPS_ELIGIBLE_STATUSES = {"tailored", "saved", "applied"}` for `!coverletter`/`!prep`; no test asserts the whitelist is *actually enforced* (i.e., `!coverletter` on a `passed` listing no-ops with a user-facing error). |
| 3 | **Medium** | `sweeper.py::_handle_tailor` | Transitions `saved` or `triaged` → `tailored`. No test verifies the transition guard (what happens on ✏️ applied to an already-`rejected` listing?). Note: `auto_rejected` is no longer a verdict; guard testing can use `rejected`/`passed` status directly. |
| 4 | **Medium** | `sweeper.py` reaction handlers | No explicit test matrix `(starting_status, reaction) → (final_status, side_effect)`. Current tests cover individual paths but not the full 5×4 grid. |
| 5 | **Medium** | `db.py::update_pipeline_status` | Rejects invalid status and nonexistent listing — tested. Does *not* enforce valid *transitions* (e.g., `passed` → `interviewing` is currently silently allowed). Product intent unclear; if intended, needs a note; if unintended, needs enforcement + test. |
| 6 | **Low** | `db.py` schema | `interviewing` is in the whitelist but no code path currently writes it. Either a feature stub or dead code — needs a decision and, if stubbed, an integration test once wired. |

### 3.3 External API Error Handling — remaining gaps

All three external surfaces (OpenRouter, IMAP, Slack) currently rely on mocked
happy-path tests. The failure modes below have production exposure:

| # | Risk | Surface | Gap |
|---|---|---|---|
| 1 | **High** | OpenRouter | `_call_openrouter` on 429 rate-limit: no test for retry/backoff behavior. Currently bubbles as a generic exception and kills the stage. |
| 2 | **High** | OpenRouter | 500/503 upstream errors: no test for fallback to a second model in the ensemble list. |
| 3 | **High** | OpenRouter | Truncated response (hit `max_tokens` mid-JSON): `_strip_code_fence` now survives the fence, but `json.loads` still fails on truncated JSON content. No test for partial-JSON recovery or re-request. |
| 4 | **High** | IMAP (`email_fetcher.py`) | Auth failure, network timeout, malformed MIME all untested. Module has no test file. |
| 5 | **Medium** | Slack | `reactions.add` errors beyond `already_reacted` (e.g., `rate_limited`, `channel_not_found`): `_mark_reply_done` swallows `already_reacted` but other errors are unverified. |
| 6 | **Medium** | Slack | `chat.update` failure in `_handle_update`: there is a graceful fallback path, but no test forces the error to verify the card is not corrupted. |
| 7 | **Medium** | Slack | `conversations.replies` pagination: large threads beyond one page are untested. |
| 8 | **Medium** | OpenRouter | `response_format={"type": "json_object"}` being silently ignored by some models that still return markdown-fenced JSON: `_strip_code_fence` handles the common case, but no test exercises a nested fence-in-fence edge case. |
| 9 | **Low** | DuckDuckGo (`research.py`) | Rate-limit / captcha response: `TestDuckDuckGoSearch` covers exception → empty, but not a captcha HTML body masquerading as results. |
| 10 | **Low** | Geopy (`geo.py`) | `geocode_failure` is tested; network timeout / rate limit is not. |

### 3.4 Combined Operations — remaining gaps

These gaps exist only when two subsystems interact. Unit tests on either side
alone do not expose them.

| # | Risk | Combination | Gap |
|---|---|---|---|
| 1 | **High** | JIT triage + email ingest | The same listing arrives via `!triage <url>` and then via the email alert a minute later. `upsert_listing` fuzzy-matches and overwrites; the JIT status reset (newly covered) applies, but the email-path `slack_notified=True` flag is *preserved* — so the second card is silently suppressed. No test covers the user-visible outcome. |
| 2 | **High** | `batch_process` + `sweeper` | `batch_process` moves a listing from `saved` → `batch_pending`. If a `!applied` lands on the Slack card during the same sweep, `_handle_applied` writes `applied` but the batch retrieval still expects `batch_pending`. No test covers the race. |
| 3 | **Medium** | `compile.py::generate_assets` + `tailor.py::_save_assets` | Both write into `outputs/<slug>/`. `_save_assets` slug is `<job_id_prefix>_<company>_<title>`; two listings with the same 8-char prefix collide silently. No test for prefix collision. |
| 4 | **Medium** | Deep Evaluation + Smart Router | A ❓ reaction triggers `_handle_smart_router`; if the thread has ChatOps commands, they are excluded from `_fetch_thread_questions`. No integration test covers a mixed thread (real questions + `!applied`). |
| 5 | **Medium** | `!update` + tailor reaction | User does `!update` to add manual context, then ✏️ to tailor. The merged `raw_email_text` feeds the tailor prompt. No test confirms the merge delimiter survives re-serialization into the LLM payload. |
| 6 | **Medium** | `sweeper` + `digest` | Digest rendering reads `pipeline_status`; a sweep mid-digest can flip status between the DB query and the Slack post. No test covers digest-vs-sweep write ordering. |
| 7 | **Low** | Profile loader + settings resolution | `llm_context` exclusion of pipeline settings is tested, but resolution order (env var > profile > default) across `geo`, `triage`, and `research` has no shared test. |
| 8 | **Low** | `report.py` + `db.py` | Report queries assume current schema; no test locks the query contract against schema migration. |

### 3.5 Untested modules (carry-forward from prior summary)

| Module | Risk | Notes |
|---|---|---|
| `src/email_fetcher.py` | **High** | IMAP fetch loop is untested. Failure modes (auth, network, malformed MIME) unverified. Also blocks §3.3 #4. |
| `src/pipeline.py` | **High** | Top-level orchestrator. No integration test covers the full fetch → classify → triage → save → notify wiring. Blocks §3.1 #6. |
| `src/jobspy_ingest.py` | **Medium** | Pre-Stage-5 dedup short-circuit and Stage 4b lazy-loader are untested. `test_jobspy_ingest.py` now collects (pandas added to dev deps). |
| `src/report.py` | **Medium** | CLI funnel report output unverified. Blocks §3.4 #8. |
| `src/models.py` | Low | Pure dataclass — behavior implicit in other tests. |

### 3.6 Partially covered behaviors (carry-forward + new)

- **`src/sweeper.py` — ChatOps command surface:** `_handle_update` merge, `_handle_smart_router`, `_handle_answers_fast`, `_extract_triage_url`, `_scan_triage_commands`, `_scan_triage_fallback_commands`, `_handle_tailor`, and all four `!trend` helpers remain untested.
- **`src/triage.py` — OpenRouter transport:** all tests mock the HTTP layer. No test verifies 429, 500, or malformed JSON handling against a captured request. (§3.3 #1–3)
- **`src/triage.py` — 4,000-char description window:** no test confirms the correct slice of `job_text` reaches the `_EVALUATE_PROMPT` `{description}` placeholder.
- **`src/triage.py` — domain-expertise skills extraction:** broadened prompt instructions are untested (no PM/healthcare fixture).
- **`src/triage.py` — `last_failure_reason`:** only set in one scrape-failure path; other failure modes leave it `None`, which the JIT fallback card relies on. No test covers the other paths.
- **`src/compile.py` — bullet edit application:** tests assert file creation but not that `_apply_executive_summary` replaces the right paragraph in a realistic multi-section `.docx`.
- **`src/db.py` — `get_trend_skills`:** new method has no tests for SQL filter correctness, LIMIT enforcement, or ordering.
- **`src/db.py` — concurrent writes / schema migration:** no tests for SQLite WAL lock handling or schema upgrade paths.
- **`src/digest.py` — threaded Deep Evaluation UX:** threaded replies, diff summary posts, and human feedback ledger writes are not covered.

---

## 4. Prioritized Sprint Plan

Five incremental sprints, ordered by production risk × blast radius. Each builds
on prior sprint fixtures where possible, so total investment drops after Sprint 2.

### Sprint A — Sweeper ChatOps completion (1 file, ~25 tests)

**Goal:** Close the remaining `sweeper.py` gaps exposed in §3.1 and §3.6.

**Scope:**
- `_scan_triage_commands` / `_scan_triage_fallback_commands`: `already_handled` broad-bot-reply false positive (§3.1 #1–2); verify a mixed thread with an unrelated warning still dispatches
- `_handle_update` merge-delimiter preserved in re-serialization (§3.4 #5); status-reset contract is **closed** (§3.1 #3)
- `_handle_smart_router` second-reaction idempotency (§3.1 #4); mixed ChatOps + questions thread (§3.4 #4)
- `_handle_answers_fast` cached-research fast-path
- `_format_trend_section` / `_format_trend_report` column alignment, top-10 truncation, empty-cohort fallback
- `_fetch_thread_questions` ChatOps prefix filter incl. `!coverletter` / `!prep`

*Completed in Sprint 2:* `_extract_triage_url`, `_classify_trend_cohort` full matrix (§3.2 #1 closed), `TestChatOpsEligibilityGate`, `TestHandleUpdateStatusContract` (§3.1 #3 closed).

**Rationale:** Highest concentration of user-facing bugs in a single module. Fixtures from `test_idempotency.py` carry over directly.

### Sprint B — External API error handling (3 files, ~20 tests)

**Goal:** Close §3.3.

**Scope:**
- `tests/test_openrouter_transport.py`: 429 retry/backoff, 500/503 ensemble fallback, truncated-JSON recovery or re-request, `response_format` ignored edge case, captured request assertions (model, temperature, max_tokens)
- `tests/test_email_fetcher.py`: IMAP auth failure, network timeout, malformed MIME, attachment handling
- Expand `test_sweeper.py` (or new `test_slack_errors.py`): `reactions.add` non-`already_reacted` errors, `chat.update` failure, `conversations.replies` pagination

**Rationale:** Production reliability floor. Every current test mocks the happy path; one upstream degradation currently halts the whole pipeline. Blocks observability improvements.

### Sprint C — State machine matrix (1 file, ~15 tests)

**Goal:** Close §3.2.

**Scope:**
- `tests/test_state_transitions.py`: full `(starting_status, reaction) → (final_status, side_effect)` matrix for all valid statuses × all ChatOps verbs
- `_CHATOPS_ELIGIBLE_STATUSES` enforcement: `!coverletter`/`!prep` on ineligible status no-ops with a user-facing error
- `_handle_tailor` guard on `rejected`/`passed` statuses (note: `auto_rejected` verdict no longer exists — use status directly)
- Decision + test for invalid transitions (e.g., `passed` → `interviewing`): either reject at `update_pipeline_status` or document as allowed
- `interviewing` status write path: either integration test once wired, or remove from whitelist if dead

**Rationale:** Prevents silent state corruption. Cheap to write once the reaction fixtures from Sprint A exist.

### Sprint D — Combined operations (mix, ~15 tests)

**Goal:** Close §3.4.

**Scope:**
- `tests/test_integration_jit_email.py`: JIT triage immediately followed by email ingest of the same listing; verify upsert + `slack_notified` contract (§3.4 #1)
- `tests/test_integration_batch_sweeper.py`: `!applied` during `batch_pending` window (§3.4 #2)
- `tests/test_asset_dir_collision.py`: `_save_assets` prefix collision on same 8-char job_id prefix (§3.4 #3)
- `tests/test_digest_sweep_race.py`: digest-query vs. sweeper-write ordering (§3.4 #6)
- Extend `test_profile_loader.py` with env > profile > default resolution test (§3.4 #7)

**Rationale:** These are the "only surfaces in production" bugs. They require more setup per test, so they come after Sprints A/B/C have built out the fixtures.

### Sprint E — Pipeline orchestration + dedup short-circuits (2 files, ~12 tests)

**Goal:** Close remaining §3.5 modules and the §3.1 #6 integration gap.

**Scope:**
- `tests/test_pipeline.py`: smoke-test full fetch → classify → triage → save → notify wiring with all externals mocked; replay/zero-side-effect assertion
- `tests/test_jobspy_ingest.py`: cover pre-Stage-5 dedup short-circuit and Stage 4b lazy loader (collection error resolved — pandas added to dev deps)
- `tests/test_report.py`: CLI funnel report output + schema-migration contract (§3.4 #8)
- `db.get_trend_skills` SQL correctness, LIMIT, ordering
- Concurrent write / WAL lock handling (spawn two connections, assert no corruption) (§3.1 #7)

**Rationale:** Finishes the coverage map. By this sprint, every fixture and mock pattern is already in place, so per-test cost is low.

### Sprint deferred / watchlist

- **Eval harness metrics (`eval/eval.py`):** the `tok/s` metric is vestigial for cloud APIs. Replace with latency percentiles when bandwidth permits.
- **Secret redaction / logging hygiene:** no test confirms API keys don't leak into logs — add a regex assertion on captured log output when a broader logging refactor happens.
- **File-system edge cases in `generate_assets`:** permission errors, unicode/special-character paths. Low-frequency but nonzero.
