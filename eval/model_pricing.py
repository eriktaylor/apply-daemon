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
(completion). The eval harness only records *total* tokens per listing, so
``blended_per_1m`` collapses the two using ``_ASSUMED_OUTPUT_FRACTION`` — a
deliberate approximation, documented here rather than hidden. If per-
direction accuracy is ever needed, log prompt/completion separately in O-1
and price them independently.
"""

from __future__ import annotations

from dataclasses import dataclass

LAST_UPDATED = "2026-07-29"
# All five rows verified against openrouter.ai on LAST_UPDATED.
PRICING_VERIFIED = True

# Assumed share of total tokens that are output/completion, used to blend the
# input and output rates into a single per-1M figure for total-token costing.
_ASSUMED_OUTPUT_FRACTION = 0.3


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens, by direction."""

    input_per_1m: float
    output_per_1m: float


# Verified 2026-07-29 against each model's openrouter.ai page.
PRICING: dict[str, ModelPrice] = {
    "openai/gpt-5.4-nano": ModelPrice(input_per_1m=0.20, output_per_1m=1.25),
    "google/gemini-3.1-flash-lite": ModelPrice(input_per_1m=0.25, output_per_1m=1.50),
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
