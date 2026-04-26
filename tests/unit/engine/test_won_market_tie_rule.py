"""Polymarket tie-rule: close ≥ strike → UP (Sprint A.1 fix)."""

from __future__ import annotations

from trading.engine.backtest_driver import _won_market
from trading.engine.types import Side


def test_strict_up_resolves_up() -> None:
    assert _won_market(open_price=100.0, final_price=100.5, side=Side.YES_UP) is True
    assert _won_market(open_price=100.0, final_price=100.5, side=Side.YES_DOWN) is False


def test_strict_down_resolves_down() -> None:
    assert _won_market(open_price=100.0, final_price=99.5, side=Side.YES_UP) is False
    assert _won_market(open_price=100.0, final_price=99.5, side=Side.YES_DOWN) is True


def test_tie_resolves_up_per_polymarket_rule() -> None:
    # The fix: close == open → UP wins.
    assert _won_market(open_price=100.0, final_price=100.0, side=Side.YES_UP) is True
    assert _won_market(open_price=100.0, final_price=100.0, side=Side.YES_DOWN) is False


def test_zero_or_negative_prices_are_false() -> None:
    # Bad data: settle dropped, both sides lose.
    assert _won_market(open_price=0.0, final_price=100.0, side=Side.YES_UP) is False
    assert _won_market(open_price=100.0, final_price=0.0, side=Side.YES_UP) is False
    assert _won_market(open_price=-1.0, final_price=100.0, side=Side.YES_UP) is False
