# Model Selection & Confidence Threshold

All LLM calls route through [OpenRouter](https://openrouter.ai), giving access to any hosted model with a single API key. Three independent model slots let you optimise cost and quality for each pipeline stage:

| Env Var | Stage | Default | Notes |
|---|---|---|---|
| `OPENROUTER_STAGE1_MODEL` | Track B extraction | `openai/gpt-5.4-nano` | Runs on every email — optimise for speed |
| `OPENROUTER_MODEL` | Stage 5 scoring (both tracks) | `google/gemini-3.1-flash-lite-preview` | Used as fallback if STAGE1 is unset |
| `OPENROUTER_TAILOR_MODEL` | Resume, cover letter, interview prep | `anthropic/claude-sonnet-4.6` | Runs only on Tailor operations |
| `OPENROUTER_TREND_MODEL` | `!trend` skill canonicalization | `openai/gpt-4o-mini` | On-demand only; 3 concurrent calls per `!trend` |

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

- **`NO` is always rejected**, regardless of confidence. A high-confidence NO is still a NO and never reaches Slack.
- **`YES` / `MAYBE`** survive only when confidence is at or above `CONFIDENCE_THRESHOLD` (a fraction between `0.0` and `1.0`, default `0.5`).

| `CONFIDENCE_THRESHOLD` | Behaviour |
|---|---|
| `0.0` | Keep every YES / MAYBE — only NO verdicts are rejected. (Equivalent to the legacy `accept_all` mode.) |
| `0.5` | **Default.** YES/MAYBE below 50% are auto-rejected; 55–75% surface as MAYBE / needs-review; ≥ 80% on a YES verdict surface as AUTO_MATCH. |
| `0.75` | Strict — only surface YES/MAYBE the model is highly confident about. |

The same value also gates AUTO_MATCH in the digest: a `YES` verdict marks as AUTO_MATCH when its confidence is at or above `max(CONFIDENCE_THRESHOLD, 0.8) × 100`%. So raising the threshold above 0.8 tightens both rejection *and* AUTO_MATCH simultaneously.

> **Migrating from `JD_REJECTION_MODE` / `OPENROUTER_ENSEMBLE_MODELS`?** The ensemble code path has been removed. Set `CONFIDENCE_THRESHOLD=0.5` to mirror the old `hard_no` cutoff (single-model NO ⇒ rejected) or `CONFIDENCE_THRESHOLD=0.0` to mirror `accept_all`. Pick a single high-quality frontier model for `OPENROUTER_MODEL` instead of voting across several. If either deprecated variable is still set in `.env`, the pipeline logs a one-time warning at startup.

## How the eval scripts interact

- `python -m eval.eval` runs whichever model your `.env` defines.
- `python -m eval.eval --model openai/gpt-4o-mini` overrides the `.env` model to benchmark a specific model in isolation. Latency and accuracy numbers reflect that model alone.
