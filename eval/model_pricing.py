"""Static OpenRouter pricing table (ranking_upgrade.md item E-1).

Hand-refreshed table of per-model token prices, used by the eval harness
(E-1/E-2) to turn token counts into a ``$ per 1k listings`` cost column
without a live network call inside the eval loop.

Every row below was cross-checked against the model's own openrouter.ai
page on ``LAST_UPDATED``. Re-verify when that date goes stale — providers
reprice, and a budget ceiling (``cli_skill_interface.md`` C-3) is only as
honest as this table. The reports print ``LAST_UPDATED`` and
``PRICING_VERIFIED`` so staleness is visible, never silent.

Note both list rates ignore prompt caching, which OpenRouter says can cut
effective cost substantially on the OpenAI and Gemini slugs. Costing at list
therefore *over*-estimates — the safe direction for a spend ceiling.

Prices are USD per 1,000,000 tokens, split into input (prompt) and output
(completion). Prefer ``cost_for_usage`` — it prices each direction exactly.

``blended_per_1m`` / ``cost_for_tokens`` remain for callers that only have a
total: the eval harness, and usage-log lines written before O-1 recorded the
split. Their ``_ASSUMED_OUTPUT_FRACTION`` was measured against a real run and
found badly wrong — the true output share is ~4% for Stage 5, not 30%, so the
blend overstated that run's cost by 2.2x. It is now set from that
measurement, but it remains an approximation: any path with per-direction
counts should use ``cost_for_usage`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass

LAST_UPDATED = "2026-07-30"
# All five rows verified against openrouter.ai on LAST_UPDATED.
PRICING_VERIFIED = True

# Share of total tokens that are output/completion, for total-only callers.
# Measured 2026-07-30 against a real 162-call run reconciled to the OpenRouter
# invoice: Stage 5 ~3.7%, Sonnet enrichment ~10%. 0.05 is a middle value that
# errs slightly high (safe for a ceiling). Fallback only — see cost_for_usage.
_ASSUMED_OUTPUT_FRACTION = 0.05


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens, by direction."""

    input_per_1m: float
    output_per_1m: float


# Verified 2026-07-29 against each model's openrouter.ai page.
PRICING: dict[str, ModelPrice] = {
    "openai/gpt-5.4-nano": ModelPrice(input_per_1m=0.20, output_per_1m=1.25),
    "google/gemini-3.1-flash-lite": ModelPrice(input_per_1m=0.25, output_per_1m=1.50),
    "google/gemini-3.5-flash-lite": ModelPrice(input_per_1m=0.30, output_per_1m=2.50),
    "openai/gpt-4o-mini": ModelPrice(input_per_1m=0.15, output_per_1m=0.60),
    "anthropic/claude-sonnet-4.6": ModelPrice(input_per_1m=3.00, output_per_1m=15.00),
    "deepseek/deepseek-v4-flash": ModelPrice(input_per_1m=0.09, output_per_1m=0.18),
}


def blended_per_1m(model: str) -> float | None:
    """Single USD-per-1M rate blending input and output prices.

    Returns None for an unknown slug so callers can flag missing pricing
    rather than silently costing at zero.
    """
    price = PRICING.get(model)
    if price is None:
        return None
    return (
        price.input_per_1m * (1 - _ASSUMED_OUTPUT_FRACTION)
        + price.output_per_1m * _ASSUMED_OUTPUT_FRACTION
    )


def cost_for_usage(model: str, prompt_tokens: float,
                   completion_tokens: float) -> float | None:
    """Exact USD for a call, priced per direction. None for unknown slugs.

    Preferred over ``cost_for_tokens``: input and output rates differ 5-6x,
    and this workload is ~96% input. The blended approximation overstated a
    real run by 2.2x, so anything with per-direction counts should use this.
    """
    price = PRICING.get(model)
    if price is None:
        return None
    return (
        prompt_tokens / 1_000_000 * price.input_per_1m
        + completion_tokens / 1_000_000 * price.output_per_1m
    )


def cost_for_tokens(model: str, total_tokens: float) -> float | None:
    """USD cost of ``total_tokens`` at ``model``'s blended rate, or None."""
    rate = blended_per_1m(model)
    if rate is None:
        return None
    return total_tokens / 1_000_000 * rate


def cost_per_1k_listings(model: str, avg_tokens_per_listing: float) -> float | None:
    """USD to process 1,000 listings at ``avg_tokens_per_listing`` each, or None."""
    per_listing = cost_for_tokens(model, avg_tokens_per_listing)
    if per_listing is None:
        return None
    return per_listing * 1000
