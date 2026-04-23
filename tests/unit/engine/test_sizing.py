"""Fractional Kelly sizing (ADR 0011)."""

from __future__ import annotations

import pytest

from trading.engine.sizing import KellyConfig, kelly_fraction, stake_for_trade


def test_kelly_fraction_zero_at_fair_coin_no_edge() -> None:
    # entry = 0.50 (fair), p = 0.50 → no edge → f* = 0
    assert kelly_fraction(0.50, 0.50) == pytest.approx(0.0, abs=1e-9)


def test_kelly_fraction_positive_when_edge_positive() -> None:
    # entry 0.40 (market says 40%), model says 60% → strong edge
    f = kelly_fraction(0.60, 0.40)
    # b = 0.6/0.4 = 1.5; f* = (1.5*0.6 - 0.4)/1.5 = (0.9 - 0.4)/1.5 = 0.333
    assert f == pytest.approx(1.0 / 3.0, abs=1e-6)


def test_kelly_fraction_clipped_to_zero_when_edge_negative() -> None:
    assert kelly_fraction(0.30, 0.50) == 0.0


def test_kelly_fraction_guards_entry_price_out_of_range() -> None:
    assert kelly_fraction(0.60, 0.0) == 0.0
    assert kelly_fraction(0.60, 1.0) == 0.0


def test_stake_uses_floor_until_min_trades_met() -> None:
    cfg = KellyConfig(stake_min_usd=5.0, stake_max_usd=15.0,
                      kelly_fraction=0.25, min_trades=20)
    # Before 20 trades, even a huge edge returns the floor stake.
    s = stake_for_trade(
        p_win=0.80, entry_price=0.40,
        capital_usd=1000.0, n_settled_trades=5, cfg=cfg,
    )
    assert s == 5.0


def test_stake_applies_kelly_after_min_trades_met() -> None:
    cfg = KellyConfig(stake_min_usd=5.0, stake_max_usd=15.0,
                      kelly_fraction=0.25, min_trades=20)
    # 20th+ trade, strong edge → Kelly bump, capped at 15.
    s = stake_for_trade(
        p_win=0.80, entry_price=0.40,
        capital_usd=1000.0, n_settled_trades=25, cfg=cfg,
    )
    # raw = 1000 * 0.25 * Kelly(0.8, 0.4); Kelly(0.8, 0.4) = (b*p - (1-p)) / b
    # b = 0.6/0.4 = 1.5; (1.5*0.8 - 0.2)/1.5 = (1.2 - 0.2)/1.5 = 0.667
    # raw = 1000 * 0.25 * 0.667 = 166.67 → clamped to 15.
    assert s == 15.0


def test_stake_clamped_to_floor_when_kelly_is_small() -> None:
    cfg = KellyConfig(stake_min_usd=5.0, stake_max_usd=15.0,
                      kelly_fraction=0.25, min_trades=20)
    # Tiny edge → Kelly tiny → raw < stake_min → clamp to min.
    s = stake_for_trade(
        p_win=0.51, entry_price=0.50,
        capital_usd=1000.0, n_settled_trades=25, cfg=cfg,
    )
    assert s == pytest.approx(5.0, abs=1e-9)


def test_stake_zero_edge_still_returns_floor_not_zero() -> None:
    cfg = KellyConfig()
    s = stake_for_trade(
        p_win=0.50, entry_price=0.50,
        capital_usd=1000.0, n_settled_trades=25, cfg=cfg,
    )
    assert s == cfg.stake_min_usd
