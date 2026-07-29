"""Unit tests for eval/model_pricing.py — E-1 static pricing table."""

from __future__ import annotations

from eval.model_pricing import (
    PRICING,
    ModelPrice,
    blended_per_1m,
    cost_for_tokens,
    cost_per_1k_listings,
)


def test_blended_rate_between_input_and_output():
    price = ModelPrice(input_per_1m=1.0, output_per_1m=11.0)
    # 0.7*1 + 0.3*11 = 4.0 with the module's assumed output fraction (0.3)
    model = "test/only"
    PRICING[model] = price
    try:
        assert blended_per_1m(model) == 4.0
    finally:
        del PRICING[model]


def test_unknown_slug_returns_none():
    assert blended_per_1m("nope/nope") is None
    assert cost_for_tokens("nope/nope", 1000) is None
    assert cost_per_1k_listings("nope/nope", 1000) is None


def test_cost_scales_with_tokens():
    model = next(iter(PRICING))
    one = cost_for_tokens(model, 1000)
    two = cost_for_tokens(model, 2000)
    assert one is not None and two is not None
    assert abs(two - 2 * one) < 1e-12


def test_cost_per_1k_listings_is_1000x_per_listing():
    model = next(iter(PRICING))
    per_listing = cost_for_tokens(model, 500)
    per_1k = cost_per_1k_listings(model, 500)
    assert per_listing is not None and per_1k is not None
    assert abs(per_1k - per_listing * 1000) < 1e-9


class TestPricingProvenance:
    """The table underwrites a spend ceiling (cli_skill_interface.md C-3), so
    its honesty is a tested property, not a comment."""

    def test_verified_flag_and_date_agree(self):
        from eval.model_pricing import LAST_UPDATED, PRICING_VERIFIED
        # If someone edits a price they must re-date the table; if they can't
        # verify it, they must drop the flag. Both together, or neither.
        assert PRICING_VERIFIED is True
        assert LAST_UPDATED == "2026-07-29"

    def test_no_zero_or_negative_rates(self):
        from eval.model_pricing import PRICING
        for slug, price in PRICING.items():
            assert price.input_per_1m > 0, slug
            assert price.output_per_1m > 0, slug

    def test_output_never_cheaper_than_input(self):
        """True of every provider's list pricing; a violation means a row was
        transcribed with the columns swapped."""
        from eval.model_pricing import PRICING
        for slug, price in PRICING.items():
            assert price.output_per_1m >= price.input_per_1m, slug

    def test_live_slugs_are_priced(self):
        """The two slugs the pipeline actually defaults to must never be
        missing — an unpriced live model costs at None, i.e. invisibly."""
        from eval.model_pricing import PRICING
        assert "google/gemini-3.1-flash-lite" in PRICING   # OPENROUTER_MODEL
        assert "openai/gpt-5.4-nano" in PRICING            # STAGE1
        assert "anthropic/claude-sonnet-4.6" in PRICING    # TAILOR
