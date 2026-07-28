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
