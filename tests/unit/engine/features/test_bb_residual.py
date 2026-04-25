"""Unit tests for the Brownian-bridge prior + 4 bb_residual features."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from trading.engine.features.bb_residual import (
    _SECONDS_PER_YEAR,
    bb_residual_features,
    brownian_bridge_prob,
)

# ---------------- pure brownian_bridge_prob -------------------------- #


def test_t_zero_delta_zero_returns_half() -> None:
    p = brownian_bridge_prob(
        spot=70_000.0, open_=70_000.0, t_in_window_s=0.0, vol_per_sqrt_s=0.001, T=300.0
    )
    assert p == pytest.approx(0.5, abs=1e-12)


def test_late_window_strong_positive_delta_saturates_to_one() -> None:
    # 1 s before close, with spot 50 bps above open and tiny vol → P→1.
    p = brownian_bridge_prob(
        spot=70_350.0, open_=70_000.0, t_in_window_s=299.0, vol_per_sqrt_s=1e-5, T=300.0
    )
    assert p > 0.999


def test_late_window_strong_negative_delta_saturates_to_zero() -> None:
    p = brownian_bridge_prob(
        spot=69_650.0, open_=70_000.0, t_in_window_s=299.0, vol_per_sqrt_s=1e-5, T=300.0
    )
    assert p < 0.001


def test_symmetry_around_half() -> None:
    common = dict(open_=70_000.0, t_in_window_s=180.0, vol_per_sqrt_s=2e-4, T=300.0)
    p_up = brownian_bridge_prob(spot=70_100.0, **common)
    p_dn = brownian_bridge_prob(spot=69_900.0, **common)
    assert p_up + p_dn == pytest.approx(1.0, abs=1e-9)


def test_open_zero_returns_half_no_crash() -> None:
    # A market without a captured open_price must not crash — neutral default.
    assert (
        brownian_bridge_prob(
            spot=70_000.0, open_=0.0, t_in_window_s=120.0, vol_per_sqrt_s=2e-4, T=300.0
        )
        == 0.5
    )


def test_vol_zero_returns_half_no_crash() -> None:
    assert (
        brownian_bridge_prob(
            spot=70_010.0, open_=70_000.0, t_in_window_s=120.0, vol_per_sqrt_s=0.0, T=300.0
        )
        == 0.5
    )


def test_t_at_close_returns_step_function() -> None:
    common = dict(open_=70_000.0, vol_per_sqrt_s=2e-4, T=300.0)
    assert brownian_bridge_prob(spot=70_100.0, t_in_window_s=300.0, **common) == 1.0
    assert brownian_bridge_prob(spot=69_900.0, t_in_window_s=300.0, **common) == 0.0
    assert brownian_bridge_prob(spot=70_000.0, t_in_window_s=300.0, **common) == 0.5


def test_result_is_in_unit_interval() -> None:
    # Sample a small grid and assert every output ∈ [0, 1].
    for t in (0.0, 60.0, 150.0, 240.0, 299.0):
        for delta in (-200.0, -50.0, -1.0, 0.0, 1.0, 50.0, 200.0):
            for vol in (1e-5, 1e-4, 5e-4, 2e-3):
                p = brownian_bridge_prob(70_000.0 + delta, 70_000.0, t, vol)
                assert 0.0 <= p <= 1.0


# ---------------- bb_residual_features(ctx) -------------------------- #


@dataclass
class _Ctx:
    """Minimal ctx fake — only the fields bb_residual_features reads.
    Imitates TickContext duck-typing without dragging in indicators init.
    """

    spot_price: float
    open_price: float
    t_in_window: float
    vol_ewma: float
    implied_prob_yes: float
    model_prob_yes: float | None


def test_features_keys_and_ranges() -> None:
    ctx = _Ctx(
        spot_price=70_010.0,
        open_price=70_000.0,
        t_in_window=180.0,
        vol_ewma=0.50,  # annualised
        implied_prob_yes=0.55,
        model_prob_yes=0.60,
    )
    f = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert set(f.keys()) == {
        "bb_p_prior",
        "bb_model_vs_prior",
        "bb_market_vs_prior",
        "bb_edge_vs_market",
    }
    assert all(math.isfinite(v) for v in f.values())
    assert 0.0 <= f["bb_p_prior"] <= 1.0


def test_vol_ewma_conversion_matches_per_sqrt_s_call() -> None:
    """bb_residual_features must convert the annualised ctx.vol_ewma to
    per-sqrt(s) before delegating to brownian_bridge_prob — mismatch
    would manifest as a factor-of-5616 error."""
    spot, open_, t = 70_010.0, 70_000.0, 180.0
    vol_annual = 0.50
    expected_p = brownian_bridge_prob(spot, open_, t, vol_annual / math.sqrt(_SECONDS_PER_YEAR))
    ctx = _Ctx(spot, open_, t, vol_annual, 0.5, 0.6)
    out = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert out["bb_p_prior"] == pytest.approx(expected_p, abs=1e-12)


def test_features_finite_when_model_is_none() -> None:
    """ctx.model_prob_yes=None must NOT produce NaN — the chosen sentinel
    is 0.0 for the two model-dependent features."""
    ctx = _Ctx(70_010.0, 70_000.0, 180.0, 0.50, 0.55, None)
    f = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert all(math.isfinite(v) for v in f.values())
    assert f["bb_model_vs_prior"] == 0.0
    assert f["bb_edge_vs_market"] == 0.0
    # The model-independent features still carry signal.
    assert math.isfinite(f["bb_p_prior"])
    assert f["bb_market_vs_prior"] == pytest.approx(0.55 - f["bb_p_prior"], abs=1e-12)


def test_features_finite_when_model_is_nan() -> None:
    ctx = _Ctx(70_010.0, 70_000.0, 180.0, 0.50, 0.55, float("nan"))
    f = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert all(math.isfinite(v) for v in f.values())
    assert f["bb_model_vs_prior"] == 0.0
    assert f["bb_edge_vs_market"] == 0.0


def test_features_with_zero_vol_returns_half_prior() -> None:
    ctx = _Ctx(70_010.0, 70_000.0, 180.0, 0.0, 0.55, 0.6)
    f = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert f["bb_p_prior"] == 0.5
    assert f["bb_market_vs_prior"] == pytest.approx(0.05, abs=1e-12)


def test_features_with_zero_open_price_returns_half_prior() -> None:
    ctx = _Ctx(70_010.0, 0.0, 180.0, 0.50, 0.55, 0.6)
    f = bb_residual_features(ctx)  # type: ignore[arg-type]
    assert f["bb_p_prior"] == 0.5
