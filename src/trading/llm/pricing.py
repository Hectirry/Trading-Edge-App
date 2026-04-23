"""OpenRouter model whitelist + pricing (ADR 0010).

Rates verified against openrouter.ai model pages on 2026-04-23. Prices
are USD per 1 000 000 tokens. Any model not in this table is rejected
at the endpoint before any HTTP call.

Keep this file in sync with ``Docs/decisions/0010-llm-copilot-openrouter.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    model: str
    in_per_million_usd: float
    out_per_million_usd: float
    context_tokens: int


MODEL_PRICING: dict[str, ModelPrice] = {
    "qwen/qwen3-max": ModelPrice(
        model="qwen/qwen3-max",
        in_per_million_usd=0.78,
        out_per_million_usd=3.90,
        context_tokens=262_144,
    ),
    "anthropic/claude-sonnet-4.6": ModelPrice(
        model="anthropic/claude-sonnet-4.6",
        in_per_million_usd=3.00,
        out_per_million_usd=15.00,
        context_tokens=1_000_000,
    ),
    "anthropic/claude-opus-4.6": ModelPrice(
        model="anthropic/claude-opus-4.6",
        in_per_million_usd=5.00,
        out_per_million_usd=25.00,
        context_tokens=1_000_000,
    ),
    "openai/gpt-4o-mini": ModelPrice(
        model="openai/gpt-4o-mini",
        in_per_million_usd=0.15,
        out_per_million_usd=0.60,
        context_tokens=128_000,
    ),
    "meta-llama/llama-3.3-70b-instruct": ModelPrice(
        model="meta-llama/llama-3.3-70b-instruct",
        in_per_million_usd=0.10,
        out_per_million_usd=0.32,
        context_tokens=131_072,
    ),
}


def allowed(model: str) -> bool:
    return model in MODEL_PRICING


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    price = MODEL_PRICING[model]
    return round(
        (tokens_in / 1_000_000) * price.in_per_million_usd
        + (tokens_out / 1_000_000) * price.out_per_million_usd,
        6,
    )
