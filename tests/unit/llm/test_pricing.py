"""Pricing whitelist + cost arithmetic (ADR 0010)."""

from __future__ import annotations

import pytest

from trading.llm.pricing import MODEL_PRICING, allowed, cost_usd


def test_default_model_is_whitelisted() -> None:
    assert allowed("qwen/qwen3-max")


def test_unknown_model_rejected() -> None:
    assert not allowed("attacker/evil-model-v7")


@pytest.mark.parametrize(
    "model, tok_in, tok_out, expected",
    [
        ("qwen/qwen3-max", 1_000_000, 1_000_000, 4.68),  # 0.78 + 3.90
        ("anthropic/claude-sonnet-4.6", 1_000_000, 1_000_000, 18.00),  # 3 + 15
        ("anthropic/claude-opus-4.6", 1_000_000, 1_000_000, 30.00),  # 5 + 25
        ("openai/gpt-4o-mini", 1_000_000, 1_000_000, 0.75),
        ("meta-llama/llama-3.3-70b-instruct", 1_000_000, 1_000_000, 0.42),
    ],
)
def test_cost_math_matches_pricing_table(model, tok_in, tok_out, expected) -> None:
    assert cost_usd(model, tok_in, tok_out) == pytest.approx(expected, abs=1e-6)


def test_all_models_have_positive_prices_and_context() -> None:
    for name, p in MODEL_PRICING.items():
        assert p.in_per_million_usd > 0, name
        assert p.out_per_million_usd > 0, name
        assert p.context_tokens >= 128_000, name
