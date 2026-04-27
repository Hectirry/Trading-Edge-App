"""Unit tests for GlobalMMState — aggregate caps across MM instances."""

from __future__ import annotations

import asyncio

from trading.paper.global_mm_state import GlobalMMState


def test_below_caps_allows_post_quote():
    state = GlobalMMState(
        max_total_capital_at_risk_usd=100.0,
        max_total_inventory_usdc=100.0,
    )
    blocked, reason = asyncio.run(
        state.block_post_quote("inst_a", delta_inventory_usdc=10, delta_capital_at_risk_usd=10)
    )
    assert blocked is False
    assert reason == "ok"


def test_inventory_cap_blocks():
    state = GlobalMMState(
        max_total_capital_at_risk_usd=1000.0,
        max_total_inventory_usdc=50.0,
    )
    asyncio.run(state.update_strategy("inst_a", inventory_usdc=40, capital_at_risk_usd=40))
    blocked, reason = asyncio.run(
        state.block_post_quote("inst_b", delta_inventory_usdc=20, delta_capital_at_risk_usd=20)
    )
    # 40 (existing inst_a) + 20 (delta inst_b) = 60 > 50 → block
    assert blocked is True
    assert "global_inventory_cap" in reason


def test_capital_cap_blocks():
    state = GlobalMMState(
        max_total_capital_at_risk_usd=50.0,
        max_total_inventory_usdc=1000.0,
    )
    asyncio.run(state.update_strategy("inst_a", inventory_usdc=10, capital_at_risk_usd=40))
    blocked, reason = asyncio.run(
        state.block_post_quote("inst_b", delta_inventory_usdc=10, delta_capital_at_risk_usd=20)
    )
    # 40 + 20 = 60 > 50 → block
    assert blocked is True
    assert "global_capital_cap" in reason


def test_excludes_self_from_double_counting():
    """Updating inst_a's exposure shouldn't double-count it when inst_a
    asks if it can post another quote."""
    state = GlobalMMState(max_total_inventory_usdc=100.0, max_total_capital_at_risk_usd=1000.0)
    asyncio.run(state.update_strategy("inst_a", inventory_usdc=80, capital_at_risk_usd=80))
    # inst_a asking to add $10 more → should evaluate against |80| + 10 = 90 ≤ 100
    blocked, _ = asyncio.run(
        state.block_post_quote("inst_a", delta_inventory_usdc=10, delta_capital_at_risk_usd=10)
    )
    assert blocked is False


def test_total_aggregations():
    state = GlobalMMState()
    asyncio.run(state.update_strategy("a", 10, 100))
    asyncio.run(state.update_strategy("b", -5, 50))
    asyncio.run(state.update_strategy("c", 7, 75))
    assert asyncio.run(state.total_inventory_usdc()) == 12  # 10 + (-5) + 7
    assert asyncio.run(state.total_capital_at_risk_usd()) == 225
