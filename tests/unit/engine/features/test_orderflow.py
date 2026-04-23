"""MLOFI, VPIN, microprice, jumps — deterministic scenarios."""

from __future__ import annotations

import math

import pytest

from trading.engine.features.jumps import lee_mykland_jump_flag
from trading.engine.features.microprice import microprice
from trading.engine.features.mlofi import OrderBookLevel, mlofi
from trading.engine.features.vpin import vpin


def _lvl(bid_p, bid_s, ask_p, ask_s) -> OrderBookLevel:
    return OrderBookLevel(bid_p, bid_s, ask_p, ask_s)


def test_mlofi_bid_improves() -> None:
    prev = [_lvl(0.48, 100, 0.52, 100)]
    now = [_lvl(0.49, 100, 0.52, 100)]
    # bid improves → +100 on bid flow, ask unchanged → 0 ask flow
    assert mlofi(prev, now)[0] == 100.0


def test_mlofi_ask_improves_is_negative() -> None:
    prev = [_lvl(0.48, 100, 0.52, 100)]
    now = [_lvl(0.48, 100, 0.51, 100)]
    # ask improves → +100 on ask flow, bid 0 → MLOFI = -100
    assert mlofi(prev, now)[0] == -100.0


def test_mlofi_size_delta_same_prices() -> None:
    prev = [_lvl(0.48, 100, 0.52, 100)]
    now = [_lvl(0.48, 150, 0.52, 80)]
    # bid size +50, ask size -20 → MLOFI = (50) - (-20) = 70
    assert mlofi(prev, now)[0] == 70.0


def test_mlofi_raises_on_level_mismatch() -> None:
    with pytest.raises(ValueError):
        mlofi([_lvl(0.48, 1, 0.52, 1)], [_lvl(0.48, 1, 0.52, 1), _lvl(0.47, 1, 0.53, 1)])


def test_vpin_all_buy_is_one() -> None:
    trades = [(0.0, 10.0, "BUY")] * 100
    v = vpin(trades, bucket_volume=50.0, n_buckets=10)
    assert v == pytest.approx(1.0, abs=1e-9)


def test_vpin_balanced_is_zero() -> None:
    # perfectly alternating buys and sells with equal qty → 0
    trades = [(0.0, 1.0, "BUY" if i % 2 else "SELL") for i in range(1000)]
    v = vpin(trades, bucket_volume=50.0, n_buckets=10)
    assert v < 0.1


def test_vpin_empty_trades() -> None:
    assert vpin([], bucket_volume=50.0) == 0.0


def test_microprice_balanced_depth() -> None:
    # symmetric book: yes_ask = 0.50, no_ask = 0.50
    mp = microprice(yes_ask=0.50, no_ask=0.50, depth_yes=100, depth_no=100)
    assert mp == pytest.approx(0.50, abs=1e-9)


def test_microprice_heavy_yes_depth_pulls_towards_sell_yes_price() -> None:
    # More depth on YES side: micro leans towards (1 - no_ask) = 0.53 here.
    mp = microprice(yes_ask=0.48, no_ask=0.47, depth_yes=900, depth_no=100)
    assert mp > 0.50
    assert mp < 0.54


def test_lee_mykland_jump_none() -> None:
    spots = [100.0 + 0.01 * math.sin(i) for i in range(120)]
    assert lee_mykland_jump_flag(spots) is False


def test_lee_mykland_jump_detected_at_end() -> None:
    spots = [100.0 + 0.001 * i for i in range(120)]
    spots[-1] = 105.0  # huge positive jump
    assert lee_mykland_jump_flag(spots) is True
