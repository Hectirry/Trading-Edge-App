"""BS-digital pricer + EWMA σ tests."""

from __future__ import annotations

import math

import pytest

from trading.engine.features.black_scholes_digital import p_up, sigma_ewma


def test_p_up_canonical_brief_example() -> None:
    """Golden vector from the user's 2026-04-26 brief.

    P_open=$67,500, P_spot=$67,520.55, τ=4 s, σ=0.03 %/√s.
    Brief states P(UP) ≈ 69.4 %.
    """
    delta_pct = (67_520.55 - 67_500.00) / 67_500.00  # +0.03044 %
    p = p_up(delta_pct=delta_pct, tau_s=4.0, sigma_per_sqrt_s=3e-4)
    assert 0.69 <= p <= 0.70  # 69.4 % per brief, allow ±0.5 pp slack


def test_p_up_collapses_at_tau_zero() -> None:
    # Window already closed — outcome deterministic in the limit.
    assert p_up(delta_pct=+0.001, tau_s=0.0, sigma_per_sqrt_s=3e-4) == 1.0
    assert p_up(delta_pct=-0.001, tau_s=0.0, sigma_per_sqrt_s=3e-4) == 0.0
    assert p_up(delta_pct=0.0, tau_s=0.0, sigma_per_sqrt_s=3e-4) == 0.5


def test_p_up_collapses_at_zero_sigma() -> None:
    # Zero vol — same step-function behaviour as τ=0.
    assert p_up(delta_pct=+0.001, tau_s=10.0, sigma_per_sqrt_s=0.0) == 1.0
    assert p_up(delta_pct=-0.001, tau_s=10.0, sigma_per_sqrt_s=0.0) == 0.0


def test_p_up_symmetric_around_zero() -> None:
    # Φ(z) + Φ(-z) = 1.
    a = p_up(delta_pct=+0.0005, tau_s=5.0, sigma_per_sqrt_s=3e-4)
    b = p_up(delta_pct=-0.0005, tau_s=5.0, sigma_per_sqrt_s=3e-4)
    assert a + b == pytest.approx(1.0, abs=1e-9)


def test_p_up_monotone_in_delta() -> None:
    # As δ increases at fixed τ, P(UP) is monotone non-decreasing.
    deltas = [-2e-3, -5e-4, 0.0, 5e-4, 2e-3]
    probs = [p_up(d, tau_s=10.0, sigma_per_sqrt_s=3e-4) for d in deltas]
    for a, b in zip(probs[:-1], probs[1:], strict=True):
        assert a <= b


def test_p_up_returns_to_half_when_sigma_dominates() -> None:
    # σ√τ ≫ |δ|: prediction collapses toward 0.5.
    p = p_up(delta_pct=1e-6, tau_s=300.0, sigma_per_sqrt_s=1e-2)
    assert 0.45 < p < 0.55


def test_sigma_ewma_short_series() -> None:
    # < 2 spots → 0 (caller's "do not enter" sentinel).
    assert sigma_ewma([]) == 0.0
    assert sigma_ewma([100.0]) == 0.0


def test_sigma_ewma_constant_series() -> None:
    # Flat price → zero vol.
    assert sigma_ewma([100.0] * 50) == 0.0


def test_sigma_ewma_known_jumps() -> None:
    # Construct a series with a single +1 % jump and verify σ > 0.
    spots = [100.0] * 30 + [101.0] + [101.0] * 10
    s = sigma_ewma(spots, lam=0.94)
    # One log-return of ln(1.01) ≈ 0.00995. EWMA decays slowly enough
    # that σ stays > 1e-3 a few steps later.
    assert s > 1e-4
    # Sanity upper bound — single 1 % jump shouldn't blow up σ above 1 %.
    assert s < 0.01


def test_sigma_ewma_drops_non_positive() -> None:
    # Non-positive spots are filtered (defensive — should never happen
    # in practice but keeps the helper safe under bad data).
    s = sigma_ewma([100.0, 0.0, -5.0, 100.5, 101.0])
    assert math.isfinite(s)
    assert s >= 0


# tick_rule_cvd ---------------------------------------------------------


def test_tick_rule_cvd_empty_or_single() -> None:
    from trading.engine.features.black_scholes_digital import tick_rule_cvd

    assert tick_rule_cvd([]) == 0.0
    assert tick_rule_cvd([100.0]) == 0.0


def test_tick_rule_cvd_all_up() -> None:
    from trading.engine.features.black_scholes_digital import tick_rule_cvd

    assert tick_rule_cvd([100.0, 100.5, 101.0, 101.5]) == 1.0


def test_tick_rule_cvd_all_down() -> None:
    from trading.engine.features.black_scholes_digital import tick_rule_cvd

    assert tick_rule_cvd([101.5, 101.0, 100.5, 100.0]) == -1.0


def test_tick_rule_cvd_balanced() -> None:
    from trading.engine.features.black_scholes_digital import tick_rule_cvd

    # 2 up moves, 2 down → net 0.
    assert tick_rule_cvd([100, 101, 100, 101, 100]) == 0.0


def test_tick_rule_cvd_flat_ticks_ignored() -> None:
    from trading.engine.features.black_scholes_digital import tick_rule_cvd

    # Flat ticks don't count as moves. 1 up, 0 flats, 0 down → +1.
    assert tick_rule_cvd([100, 100, 100, 101]) == 1.0
    # All flat → 0 moves → 0.0.
    assert tick_rule_cvd([100, 100, 100]) == 0.0
