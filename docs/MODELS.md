# Model Selection & Confidence Threshold

All LLM calls route through [OpenRouter](https://openrouter.ai), giving access to any hosted model with a single API key. Three independent model slots let you optimise cost and quality for each pipeline stage:

| Env Var | Stage | Default | Notes |
|---|---|---|---|
| `OPENROUTER_STAGE1_MODEL` | Track B extraction | `openai/gpt-5.4-nano` | Runs on every email — optimise for speed |
| `OPENROUTER_MODEL` | Stage 5 scoring (both tracks) | `google/gemini-3.1-flash-lite` | Used as fallback if STAGE1 is unset |
| `OPENROUTER_TAILOR_MODEL` | Resume, cover letter, interview prep | `anthropic/claude-sonnet-4.6` | Runs only on Tailor operations |
| `OPENROUTER_TREND_MODEL` | `!trend` skill canonicalization | `openai/gpt-4o-mini` | On-demand only; 3 concurrent calls per `!trend` |

## The session route (subscription-billed)

Some work does not go through OpenRouter at all. `src/claude_cli.py` shells out to `claude -p --model X --output-format json`, which starts a headless Claude session billed to your **Claude subscription** rather than metered credit.

| Env Var | Default | Notes |
|---|---|---|
| `AUTOPILOT_RESCORE_VIA` | `session` | `session` = subscription; `api` = OpenRouter. Same vocabulary as `cli tailor --via`. |
| `CLAUDE_CLI_MODEL` | `sonnet` | Which model the session route asks for. |

**Where it applies.** The autopilot post-research re-score (~97% of per-listing enrichment cost, at most `AUTOPILOT_TOP_N` calls a day), the `cli tailor` / `polish` / `cover-letter` / `interview-prep` / `answers` verbs, and `eval.listwise_compare --via-claude`.

**Where it deliberately does not.** Stage 5 scoring. Each CLI invocation carries the harness's own system prompt (~23k tokens, measured 2026-07-30), which is irrelevant against a handful of large calls and decisive against 100+ small ones.

**Cost accounting.** Session spend is *not* written to `logs/model_usage.log`. That file is the basis for the daily ceiling in `src/budget.py`, and counting subscription work there would make the cap refuse runs over money nobody was charged. The reported cost is logged at INFO for visibility. `tests/test_model_usage.py::TestSubscriptionSpendIsNotMetered` pins this.

**Failure behaviour.** Every caller has a metered fallback and `claude_cli.run` never raises — a missing binary, non-zero exit, timeout, or malformed envelope all return `ok=False`. A cron environment without the CLI on `PATH` silently pays OpenRouter instead of losing the listing.

## Pending: model-slot review

**`OPENROUTER_TAILOR_MODEL` is pinned to `anthropic/claude-sonnet-4.6`, which is a generation behind.** It backs the tailor family, the autopilot re-score, and (via `_rank_model` fallback) `rank_stage5`.

Deliberately *not* upgraded during the 2026-08 pilot freeze: a slug change moves the V-22 baseline mid-measurement. Revisit after the freeze, and note the session route may retire the question — once the re-score and tailoring run on the subscription, this slot is only the fallback path.

**Before changing any Stage 5 or ranking slug**, run the gate — `python -m eval.listwise_compare --gold --shuffle --model <slug>`. Measured basis: `gemini-3.5-flash-lite` was worse *and* dearer than the incumbent, and `gpt-5.4-nano` collapsed to 58%. Version numbers are not fitness. Also re-verify pricing at openrouter.ai; the table in `eval/model_pricing.py` was 2.5–4× wrong for two production models before it was checked.

## Anthropic BYOK

OpenRouter [Bring-Your-Own-Key](https://openrouter.ai/docs/guides/overview/auth/byok) is configured **server-side via the OpenRouter dashboard**, not via per-request HTTP headers or environment variables. Setting `ANTHROPIC_API_KEY` in `.env` alone does NOT enable BYOK — Apply Daemon will log a warning if you do that without dashboard configuration.

**To enable BYOK:**

1. Visit [openrouter.ai/settings/integrations](https://openrouter.ai/settings/integrations)
2. Add your Anthropic API key under **Anthropic**
3. (Optional) Toggle **"Always use this key"** to disable fallback to OpenRouter shared credits

Once configured, OpenRouter automatically forwards Anthropic-model requests through your key. You pay Anthropic at their flat API rate; OpenRouter charges a 5% routing fee against your credit balance (waived for the first 1M BYOK requests/month). The model slug (`OPENROUTER_TAILOR_MODEL`) and the rest of the pipeline are unchanged.

**Verification:** After dashboard setup, your OpenRouter activity dashboard should show requests as "BYOK" rather than billed against credits. If you're still seeing standard OpenRouter charges for Anthropic models, the dashboard step was missed.

## Confidence Threshold

Stage 5 scoring runs a single call to `OPENROUTER_MODEL` and returns a verdict (`YES` / `MAYBE` / `NO`) and a confidence percentage. Rejection rules:

- **`NO` is always rejected**, regardless of confidence. A high-confidence NO is still a NO.
- **`YES` / `MAYBE`** survive only when confidence is at or above `CONFIDENCE_THRESHOLD` (a fraction between `0.0` and `1.0`; the code falls back to `0.5` when unset, and `.env.example` ships `0.40`).

**Rejection means deletion.** A listing below the threshold is never written to the database — it cannot be reviewed, ranked, or recovered later, and nothing logs its passing. This is the knob's real weight: it is a *keep/discard* decision, not a display filter. The separate `NOISE_FLOOR_PCT` decides what autopilot spends enrichment on, and being wrong there costs nothing — the row stays queryable. Keep `CONFIDENCE_THRESHOLD` below the bottom band of your profile's ranking ladder so "ranked last" never silently becomes "deleted"; the interaction is worked through in [`docs/PROFILE.md`](PROFILE.md).

| `CONFIDENCE_THRESHOLD` | Behaviour |
|---|---|
| `0.0` | Keep every YES / MAYBE — only NO verdicts are rejected. (Equivalent to the legacy `accept_all` mode.) |
| `0.40` | **Template default.** Low floor for profiles that rank across a ladder of bands; the bottom bands survive as rows while `NOISE_FLOOR_PCT` (e.g. `55`) keeps enrichment spend at the top. |
| `0.5` | Code fallback when unset. YES/MAYBE below 50% are deleted; ≥ 80% on a YES verdict surfaces as AUTO_MATCH. |
| `0.75` | Strict — only keep YES/MAYBE the model is highly confident about. Everything else is deleted, not hidden. |

The same value also gates AUTO_MATCH in the digest: a `YES` verdict marks as AUTO_MATCH when its confidence is at or above `max(CONFIDENCE_THRESHOLD, 0.8) × 100`%. So raising the threshold above 0.8 tightens both rejection *and* AUTO_MATCH simultaneously.

> **Migrating from `JD_REJECTION_MODE` / `OPENROUTER_ENSEMBLE_MODELS`?** The ensemble code path has been removed. Set `CONFIDENCE_THRESHOLD=0.5` to mirror the old `hard_no` cutoff (single-model NO ⇒ rejected) or `CONFIDENCE_THRESHOLD=0.0` to mirror `accept_all`. Pick a single high-quality frontier model for `OPENROUTER_MODEL` instead of voting across several. If either deprecated variable is still set in `.env`, the pipeline logs a one-time warning at startup.

## How the eval scripts interact

- `python -m eval.eval` runs whichever model your `.env` defines.
- `python -m eval.eval --model openai/gpt-4o-mini` overrides the `.env` model to benchmark a specific model in isolation. Latency and accuracy numbers reflect that model alone.
