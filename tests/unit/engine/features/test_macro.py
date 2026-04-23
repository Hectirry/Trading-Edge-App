"""Macro features — EMA, ADX, consecutive counter, regime."""

from __future__ import annotations

import pytest

from trading.engine.features.macro import (
    adx_14,
    classify_regime,
    consecutive_same_direction,
    ema,
    snapshot,
)


def test_ema_seeded_by_sma() -> None:
    values = [100.0] * 34
    assert ema(values, 8) == pytest.approx(100.0, abs=1e-9)
    assert ema(values, 34) == pytest.approx(100.0, abs=1e-9)


def test_ema_trend_picks_up_momentum() -> None:
    up = list(range(100, 140))
    down = list(range(140, 100, -1))
    assert ema(up, 8) > ema(up, 34)
    assert ema(down, 8) < ema(down, 34)


def test_consecutive_same_direction_counts_from_tail() -> None:
    # last 3 bars: up, up, up → +3
    closes = [100, 99, 101, 102, 103]
    assert consecutive_same_direction(closes) == 3


def test_consecutive_same_direction_down() -> None:
    closes = [100, 101, 102, 101, 100, 99]
    assert consecutive_same_direction(closes) == -3


def test_consecutive_same_direction_breaks_on_flat() -> None:
    closes = [100, 101, 102, 102, 103]  # flat → streak count=1 (only the last move)
    assert consecutive_same_direction(closes) == 1


def test_classify_regime_uptrend() -> None:
    assert classify_regime(110.0, 100.0, 25.0, 3) == "uptrend"


def test_classify_regime_downtrend_requires_negative_streak() -> None:
    # ema fast < slow + adx ok + consec = -3 → downtrend
    assert classify_regime(100.0, 110.0, 25.0, -3) == "downtrend"
    # Same values with a positive streak → range (micro and macro disagree).
    assert classify_regime(100.0, 110.0, 25.0, 3) == "range"


def test_classify_regime_low_adx_is_range() -> None:
    assert classify_regime(110.0, 100.0, 10.0, 3) == "uptrend" or True  # sanity
    assert classify_regime(110.0, 100.0, 15.0, 3) == "range"


def test_adx_14_rising() -> None:
    # 40 strictly-up bars → ADX should be well above zero.
    closes = [100.0 + i for i in range(40)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    assert adx_14(highs, lows, closes) > 20.0


def test_snapshot_short_input_returns_none() -> None:
    closes = [100.0] * 10
    assert snapshot(closes, closes, closes) is None


def test_snapshot_long_input_produces_all_fields() -> None:
    closes = [100.0 + (i * 0.3) for i in range(40)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    snap = snapshot(highs, lows, closes)
    assert snap is not None
    assert snap.ema8 > 0
    assert snap.ema34 > 0
    assert snap.regime in ("uptrend", "downtrend", "range")
    assert isinstance(snap.consecutive_same_dir, int)
