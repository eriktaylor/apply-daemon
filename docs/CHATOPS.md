# ChatOps & Commands

The entire post-triage workflow is driven by Slack reactions and thread commands. The sweeper polls every 2 minutes — no Socket Mode or webhooks required.

## How reactions work

Each digest message includes a reaction legend. No buttons or Socket Mode required — just react on the message:

| Reaction | Action | Result |
|----------|--------|--------|
| :thumbsup: | **Save** | Status → `saved`, bot adds :white_check_mark: receipt |
| :thumbsdown: | **Pass / Expire** | Status → `passed`, message replaced with gray "Passed". Use 👎 to signal "not interested" OR "role no longer available" — both cases are handled the same way (see [Pass and Expire](#pass-and-expire)) |
| :pencil2: | **Tailor** | Bot adds :eyes:, runs Deep Research + Claude, generates targeted resume + match analysis, swaps to :white_check_mark: |
| :grey_question: | **Smart Router** | See [Smart Router](#the-smart-router---answer) — routes to tailor or answers depending on context |

**Reaction priority — `pass` > `tailor` > `save`:**

Reactions accumulate on the card over time, so the sweeper enforces an explicit priority order. Per sweep, **only the highest-priority reaction present fires** — lower-priority co-reactions are no-ops. This means:

- 👍 then ✏️ → tailor runs (save is implicit). On the next sweep, the lingering 👍 doesn't re-fire save.
- ✏️ then 👎 → pass wins; tailor does **not** re-run, even though ✏️ is still on the card.
- 👍 on an already-tailored listing → no-op (going "backwards" in priority is silently ignored).
- 👎 on any listing → terminal pass, regardless of any 👍/✏️ also on the card.

The Smart Router (❓) is orthogonal to the save/tailor/pass chain and dispatches independently — except when 👎 wins (which removes the listing from active processing).

**Reviving a passed listing:** Pass is the highest-priority "remove" signal, so once a listing is passed, reactions on its card are no-ops. To bring a passed listing back into the active feed, post `!triage <original-url>` in the channel — this re-scrapes, re-scores, and resets `pipeline_status` to `triaged` so 👍/✏️/👎 work normally again. See [Manual Ingestion](#manual-ingestion-triage-url) for details.

Saved listings are also automatically tailored in bulk when the daily batch processor runs — all concurrent OpenRouter requests complete before the process exits.

> **Output directory:** Tailored assets (targeted resume, match analysis, and on-demand cover letter/interview prep) are saved locally to `output/<Company>_<Title>_<ID>/`. Each job gets its own directory with ready-to-send `.docx` files and a JSON dump of the full Claude response.

## Token-Optimized Default Pipeline

The default ✏️ Tailor run generates only the **core assets** to minimize API costs:
- Deep Research context (cached for later use)
- Targeted Resume (`.docx` with surgical bullet edits)
- Match Analysis (3-part evaluation with post-research verdict)
- Training data dumps (`original_triage.json`, `tailored_analysis.json`)

Cover letter and interview prep are available **on demand** via thread commands, reusing cached research context at a fraction of the token cost.

## State-Tracking Commands

Reply in a job's Slack thread to update its pipeline status. The bot updates the database and appends a visible status badge to the original message.

| Command | New Status | Badge | Notes |
|---------|------------|-------|-------|
| `!applied` | `applied` | :green_circle: APPLIED | |
| `!pass` | `passed` | :no_entry_sign: PASSED | Not interested. Cooldown: `pass_window_days` (default 180 days) |
| `!expire` / `!expired` | `expired` | :no_entry_sign: EXPIRED | Role filled or removed. Same cooldown behavior as `!pass`. Works from any active status including `triaged`. Both spellings are accepted. |
| `!interview` | `interviewing` | :star2: INTERVIEWING | |
| `!rejected` | `rejected` | :red_circle: REJECTED | |

`!applied`, `!pass`, `!interview`, and `!rejected` work on jobs in the `tailored`, `saved`, or `applied` states. `!expire` works from any active status. Each transition is logged to the human feedback ledger.

## Pass and Expire

Both 👎 and `!expire` produce a 🚫 badge in Slack and block the listing from re-entering your digest — the difference is semantic:

- **Pass** (`!pass` / 👎) — you're not interested in the role.
- **Expire** (`!expire` / `!expired`) — the role is no longer available (filled, removed, or posting gone 404).

**Cooldown:** Both statuses respect `pass_window_days` (default 180 days, set in `profile.md` Pipeline Settings). After the window expires, the same job title + company at that employer can resurface in a future digest cycle — useful if the company repost the same role or a new opening at the same team appears. Within the window, all job board scrapes and email alerts are silently de-duplicated against the blocked listing.

**`!expire` from `triaged`:** Unlike other state commands, `!expire` is available even on freshly triaged listings (e.g., you open the digest, see the company posted a role but the URL is already 404'd — hit `!expire` immediately without needing to save or tailor first).

## On-Demand Asset Generation

Generate expensive assets only when you need them. All commands load cached context from the output folder — no re-scraping.

| Command | Asset Generated | Notes |
|---------|----------------|-------|
| `!coverletter` | `Cover_Letter_{Company}[_vN].docx` | Uses cached research + prior tailor analysis |
| `!prep` | `Interview_Prep_{Company}[_vN].md` | Uses cached research + prior tailor analysis |
| `!polish` | `Polished_Resume_{Company}[_vN].docx` | Requires a completed tailor pass (`pipeline_status == tailored`). Integrates the executive summary rewrite and bullet edits into a single cohesive final document. Zero-hallucination audit: only rearranges/refines content from the base resume and tailor edits. |

Repeat calls produce versioned output (`_v2`, `_v3`, …) so prior versions are never overwritten.

The bot replies in the thread with a confirmation (e.g., *":white_check_mark: Generated polished resume"*) and the output path.

## Regeneration (`!regenerate`)

Force a complete asset rebuild for a tailored role — delete all existing output and run the full pipeline from scratch.

**When to use:**

- You deleted a specific asset (e.g., the resume) and want everything regenerated fresh.
- A previous tailor run failed silently (the sweeper will surface this via a `:warning:` message with a `!regenerate` hint).
- You want to incorporate profile updates or a new base resume into an existing tailored role.

**Requirements:** The ✏️ (pencil) emoji must be on the card. `!regenerate` is rejected for passed listings — use `!triage <url>` to revive the listing first.

**How it works:** The sweeper deletes the output folder, resets the listing to `triaged`, and immediately fires a full fresh tailor (deep research + LLM + asset compile) in the same sweep pass. A ↩️ receipt is applied to your reply for idempotency — subsequent sweeps ignore it.

> **Checkpoint fallback (automatic):** You don't always need `!regenerate`. The sweeper silently detects three failure states when pencil is present and `pipeline_status == "tailored"`:
> - **No output folder** → auto-regenerates on the next sweep.
> - **`deep_research_context.txt` missing** → posts a `:warning:` error and prompts you to `!regenerate`.
> - **`assets.json` missing** (research completed but LLM/compile failed) → auto-resumes using the cached research context, no re-scraping.

## The Smart Router (`❓` / `!answer`)

The ❓ reaction and `!answer` command provide a unified entry point for custom application questions ("Why do you want to work here?", "Describe a time you...").

**How it works:**

1. **❓ with no text in thread** → Fallback: treated as ✏️ Tailor (generates default assets, updates state to tailored).

2. **❓ with text in thread** OR **`!answer <questions>`** → The router checks the job's current status:
   - **Route A (status: NEW or SAVED):** Runs the full pipeline — Deep Research + Claude generates default assets PLUS custom answers in one pass. Answers saved as `Custom_Answers.md`. State → tailored.
   - **Route B (status: TAILORED, APPLIED, etc.):** Fast path — loads cached research, calls Claude with `max_tokens: 1024` for answers only. No re-scraping, no re-tailoring. Answers posted in thread and saved as `Custom_Answers.md`.

**Idempotency:** After the sweeper processes an `!answer` command, it places a ✅ reaction on that specific reply. On every subsequent sweep, the sweeper checks for that reaction and skips the reply — preventing the API from being called again. Post a new `!answer <text>` reply any time to ask follow-up questions; the new message has no ✅ and will be processed fresh.

**Example workflow:**
```
1. React ✏️ on a listing              → Default assets generated (resume + analysis)
2. Reply: !answer Why this company?
          What's your management style?  → Sweeper processes, edits to !answered ...
3. Later: !answer Tell me about the eng culture  → New follow-up, processed fresh
```

## Manual Ingestion (`!triage <URL>`)

Post a job posting URL directly in the main channel:

```
!triage https://careers.stripe.com/listing/senior-backend-engineer
```

The bot scrapes the page and runs the full LLM pipeline (Stage 1 extraction → Stage 5 scoring). The scraper uses a 5-second fail-fast timeout with no retries — it will not hang on anti-bot pages.

**Smart Upsert:** After triage, the bot checks if a fuzzy-matching listing (same title + company) already exists in the database within the `dedup_window_days` window. If a match is found, it **overwrites** the data fields (description, skills, score) and **resets `pipeline_status` to `triaged`** — fully reviving listings stuck in terminal states. The Slack card shows a `[ 🔄 Overwrote existing truncated listing ]` badge when this happens.

**Reviving a passed listing:** If you previously hit 👎 on a job and later change your mind, just `!triage <url>` again. The new card appears in your feed with status reset to `triaged`, so 👍/✏️/👎 reactions work normally on the revived card. This closes the priority loop — passed is the highest-priority "remove" signal, but `!triage` is always the escape hatch.

### Threaded Fallback Workflow (scrape blocked)

When the page cannot be scraped (Cloudflare, ATS JavaScript wall, 429), the bot posts a warning in the thread instead of failing silently:

```
⚠️ Could not scrape that URL. Reply to this thread with `!update <job description text>` to parse this job manually.
```

**To recover:** copy the job description text from your browser, then reply to that warning thread:

```
[In thread of the !triage warning]
!update Product Manager, Health Systems

Howdy, we're Heidi 👋 ...full job description pasted here...
```

The sweeper picks up the `!update` reply, runs the full Stage 1-5 pipeline on the pasted text (using the original URL as the listing link), and posts the scored card to the channel — identical to a successful `!triage`. **No database row is created until the LLM successfully structures the data**, so there are no stubs or orphaned records from failed scrapes.

**Late deduplication:** The DB check runs *after* the LLM returns structured data. If the job was already ingested via a different track, the bot posts an `🔄 Duplicate detected — updated existing record` notice in the thread and overwrites the existing listing with the richer pasted text.

**Idempotency:** The bot places ✅ on the processed `!update` reply. Subsequent sweeps skip it. Post a new `!update <text>` reply any time to re-score with additional context.

## Context Update (`!update <text>`)

`!update` works in two contexts:

**Context A — Existing job card thread** (enrichment / re-score):

```
[In thread of an existing job card]
!update Senior Backend Engineer at Stripe. Responsibilities: build and scale...
        ...paste the full job description here...
```

Merges the new text with the existing job description. The LLM receives both the original description and your pasted text, separated by a `--- ADDITIONAL MANUAL CONTEXT ---` delimiter so historical context (prior matched skills, original JD) is preserved in the re-score. The original Slack card is updated in-place.

Use this when:
- The listing was initially triaged from a truncated email excerpt and you now have the full JD
- You want to annotate the listing with additional role context before tailoring

**Context B — !triage warning thread** (scrape-failure recovery):

When `!triage <URL>` fails to scrape, reply to the warning message with `!update <text>` to trigger a full fresh triage on the pasted text. See [Threaded Fallback Workflow](#threaded-fallback-workflow-scrape-blocked) above.

After processing in either context, the sweeper places a ✅ reaction on the reply to prevent re-firing. Post a new `!update <text>` reply any time to add further context.

## Labor Market Intelligence (`!trend`)

Post `!trend` in the main channel to get a skill frequency report across your most recent scored jobs:

```
!trend                # default: last 100 jobs
!trend --deep 250     # widen the window (10 ≤ N ≤ 500)
```

The bot splits the last *N* scored jobs into three cohorts and posts a monospace trend report as a thread reply:

| Cohort | Definition |
|--------|-----------|
| **High Intent** | `pipeline_status` in `saved / tailored / applied / interviewing` |
| **Pipeline** | Scored YES or MAYBE but not yet saved (`triaged`) |
| **Rejected** | Scored NO or user-passed (`passed / rejected`) |

For each cohort, the report shows **top 10 matched skills** (skills you have that the role requires) and **top 10 missing skills** (skill gaps). Each row also shows the share of that cohort that mentioned the skill, so you can compare across cohorts of different sizes (e.g. "Python in High Intent at 60%" vs. "Python in Rejected at 18%").

Before counting, the bot sends each cohort's raw skill arrays to `OPENROUTER_TREND_MODEL` using three concurrent async calls — the LLM groups synonymous terms together (e.g., `GenAI` / `Generative AI` / `LLMs` → `Generative AI / LLMs`) so the frequencies reflect true demand rather than tokenization artifacts. Sentinel placeholders such as `None explicitly stated` are filtered out before counting.

### When to use `--deep`

The default 100-job window is roughly one busy day's worth of agent output. Reach for `--deep N` when:

- The agent posted a heavy batch (100+ jobs in a single push) and you want trends across multiple days.
- You want to see how the gap distribution evolves with more samples (smaller cohorts can be noisy).

The LLM input size scales with the number of *unique* skills, not raw row count, so cost is roughly flat as you increase N. `OPENROUTER_TREND_MODEL=openai/gpt-4o-mini` (default) is sufficient up to `--deep 500`; the bot also raises the canonicalization output budget automatically when `N > 200`. Values outside `[10, 500]` are clamped.

**Idempotency:** The bot adds a ✅ reaction to the `!trend` message after posting. Subsequent sweeps detect the existing bot thread reply and skip it. Each distinct `!trend` message gets one report — to re-run with a different window, post a new message (`!trend --deep 300`), don't edit the old one.

**Example output (monospace block in thread):**
```
SKILL TRENDS — Last 250 Jobs
High Intent: 38  │  Pipeline: 47  │  Rejected: 165

── HIGH INTENT  (38 jobs) ──
   Saved / Tailored / Applied

   Matched (top 10)                        Missing / Gaps (top 10)
   -------------------------------------   -------------------------------------
   Python                        24  63%   Kubernetes                    11  29%
   Agentic AI Systems            18  47%   Causal Inference               4  11%
   Forward Deployed Engineering  16  42%   RLHF                           3   8%
   Machine Learning              14  37%   Scalable Oversight             3   8%
   ...
```
