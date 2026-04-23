"""Micro features — deterministic math + no-peek invariants."""

from __future__ import annotations

import pytest

from trading.engine.features.micro import (
    momentum_bps,
    realized_vol_yz,
    tick_up_ratio,
)


def test_momentum_bps_up() -> None:
    # 90 equal samples of 100 then 101 now: 100 → 100 → ... → 101 (90 samples ago = 100)
    spots = [100.0] * 90 + [101.0]  # len=91, index -91 is 100, index -1 is 101
    assert momentum_bps(spots, 90) == pytest.approx(100.0, abs=1e-6)


def test_momentum_bps_down_and_sign() -> None:
    spots = [100.0] * 90 + [99.0]
    assert momentum_bps(spots, 90) == pytest.approx(-100.0, abs=1e-6)


def test_momentum_bps_short_buffer_returns_zero() -> None:
    assert momentum_bps([100.0] * 30, 90) == 0.0


def test_momentum_bps_handles_zero_past_price() -> None:
    spots = [0.0] + [100.0] * 90
    assert momentum_bps(spots, 90) == 0.0


def test_momentum_bps_only_reads_tail_no_leak() -> None:
    """If we append future data, the result for an earlier lookback must
    not change — momentum reads the last (lookback+1) samples only."""
    base = [100.0] * 89 + [100.5, 101.0]  # 91 samples
    a = momentum_bps(base, 90)
    extended = base + [999.0, 42.0]  # future noise
    b = momentum_bps(extended[: len(base)], 90)
    assert a == b


def test_realized_vol_yz_monotonic() -> None:
    quiet = [100.0 + 0.01 * i for i in range(91)]
    noisy = [100.0 + (0.5 if i % 2 else -0.5) for i in range(91)]
    assert realized_vol_yz(noisy, 90) > realized_vol_yz(quiet, 90)


def test_tick_up_ratio_all_up() -> None:
    spots = [float(i) for i in range(91)]  # strictly increasing
    assert tick_up_ratio(spots, 90) == pytest.approx(1.0)


def test_tick_up_ratio_all_down() -> None:
    spots = [100.0 - i for i in range(91)]
    assert tick_up_ratio(spots, 90) == 0.0


def test_tick_up_ratio_mixed() -> None:
    spots = [100.0 + (0.1 if i % 2 else -0.1) for i in range(91)]
    # alternating → 45 ups out of 90
    assert tick_up_ratio(spots, 90) == pytest.approx(45 / 90, abs=1e-6)
