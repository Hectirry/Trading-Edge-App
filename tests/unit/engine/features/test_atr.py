"""ATR unit tests (Phase 3.8a)."""

from __future__ import annotations

import pytest

from trading.engine.features.atr import ATR, compute_atr


def test_atr_insufficient_data_returns_zero() -> None:
    atr = ATR(period=14)
    for _ in range(5):
        atr.update(100.0, 99.0, 99.5)
    assert atr.ready is False
    assert atr.value == 0.0


def test_atr_constant_series_converges_to_hl_range() -> None:
    atr = ATR(period=3)
    # Constant H=101, L=99 every bar -> TR=2 after the first seeding bar.
    values = []
    for _ in range(10):
        values.append(atr.update(101.0, 99.0, 100.0))
    # Final ATR should be 2.0 (Wilder smoothing of constant TR=2 → 2).
    assert pytest.approx(values[-1], rel=1e-9) == 2.0


def test_atr_seed_uses_simple_mean() -> None:
    atr = ATR(period=3)
    # Feed exactly 3 bars; last one seeds the ATR as simple average.
    atr.update(10.0, 9.0, 9.5)
    atr.update(11.0, 9.5, 10.5)  # TR = max(1.5, 1.5, 0.0) = 1.5
    atr.update(12.0, 10.0, 11.5)  # TR = max(2.0, 1.5, 0.5) = 2.0
    # TRs: [1.0 (h-l first bar), 1.5, 2.0]; mean = 1.5
    assert pytest.approx(atr.value, rel=1e-9) == 1.5
    assert atr.ready is True


def test_compute_atr_batch_matches_stateful() -> None:
    highs = [10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15, 14, 13, 12]
    lows = [9, 10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 14, 13, 12, 11]
    closes = [
        9.5,
        10.5,
        11.5,
        12.5,
        11.5,
        10.5,
        9.5,
        10.5,
        11.5,
        12.5,
        13.5,
        14.5,
        13.5,
        12.5,
        11.5,
    ]
    batch = compute_atr(highs, lows, closes, period=14)

    stateful = ATR(period=14)
    for h, lo, c in zip(highs, lows, closes, strict=True):
        stateful.update(h, lo, c)

    assert pytest.approx(batch, rel=1e-12) == stateful.value
    assert batch > 0.0


def test_compute_atr_too_few_bars_returns_zero() -> None:
    # period+1 = 15 required; 10 bars must short-circuit to 0.0.
    n = 10
    highs = [10.0 + i for i in range(n)]
    lows = [9.0 + i for i in range(n)]
    closes = [9.5 + i for i in range(n)]
    assert compute_atr(highs, lows, closes, period=14) == 0.0


def test_compute_atr_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        compute_atr([1.0, 2.0], [1.0], [1.0, 2.0], period=1)


def test_compute_atr_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        compute_atr([1.0, 2.0], [0.5, 1.0], [1.0, 1.5], period=0)
