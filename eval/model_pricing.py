"""Static OpenRouter pricing table (ranking_upgrade.md item E-1).

Hand-refreshed table of per-model token prices, used by the eval harness
(E-1/E-2) to turn token counts into a ``$ per 1k listings`` cost column
without a live network call inside the eval loop.

IMPORTANT — the numbers below are UNVERIFIED PLACEHOLDERS. Before trusting
any cost figure, cross-check each slug against https://openrouter.ai/models
and set ``PRICING_VERIFIED = True``. The eval report prints ``LAST_UPDATED``
and the verified flag so staleness is visible, never silent.

Prices are USD per 1,000,000 tokens, split into input (prompt) and output
(completion). The eval harness only records *total* tokens per listing, so
``blended_per_1m`` collapses the two using ``_ASSUMED_OUTPUT_FRACTION`` — a
deliberate approximation, documented here rather than hidden. If per-
direction accuracy is ever needed, log prompt/completion separately in O-1
and price them independently.
"""

from __future__ import annotations

from dataclasses import dataclass

LAST_UPDATED = "2026-07-12"
# Flip to True only after cross-checking every row against openrouter.ai/models.
PRICING_VERIFIED = False

# Assumed share of total tokens that are output/completion, used to blend the
# input and output rates into a single per-1M figure for total-token costing.
_ASSUMED_OUTPUT_FRACTION = 0.3


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens, by direction."""

    input_per_1m: float
    output_per_1m: float


# PLACEHOLDER prices — verify before trusting cost columns (see module docstring).
PRICING: dict[str, ModelPrice] = {
    "openai/gpt-5.4-nano": ModelPrice(input_per_1m=0.05, output_per_1m=0.40),
    "google/gemini-3.1-flash-lite": ModelPrice(input_per_1m=0.10, output_per_1m=0.40),
    "openai/gpt-4o-mini": ModelPrice(input_per_1m=0.15, output_per_1m=0.60),
    "anthropic/claude-sonnet-4.6": ModelPrice(input_per_1m=3.00, output_per_1m=15.00),
    "deepseek/deepseek-v4-flash": ModelPrice(input_per_1m=0.05, output_per_1m=0.30),
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
