"""Step 1 tests: AS primitives for binary markets."""

from __future__ import annotations

import math

import pytest

from trading.strategies.polymarket_btc15m._mm_features import (
    ASParams,
    calibrate_c_b,
    in_active_zone,
    in_quoting_window,
    optimal_bid_ask,
    optimal_total_spread,
    reservation_price,
    sigma_bm,
)


# ─── sigma_bm ────────────────────────────────────────────────────────────


def test_sigma_bm_zero_at_terminal():
    """σ → 0 when t = T (resolution moment)."""
    assert sigma_bm(0.5, 900, 900) == 0.0
    assert sigma_bm(0.18, 900, 900) == 0.0


def test_sigma_bm_zero_after_terminal():
    """σ stays 0 past the window — defensive."""
    assert sigma_bm(0.5, 901, 900) == 0.0
    assert sigma_bm(0.5, 10000, 900) == 0.0


def test_sigma_bm_zero_at_p_extremes():
    """σ = 0 when p is degenerate (already resolved)."""
    assert sigma_bm(0.0, 100, 900) == 0.0
    assert sigma_bm(1.0, 100, 900) == 0.0
    assert sigma_bm(-0.1, 100, 900) == 0.0
    assert sigma_bm(1.1, 100, 900) == 0.0


def test_sigma_bm_max_at_t_zero_p_half():
    """At t=0 and p=0.5, σ = sqrt(0.5·0.5·1) = 0.5."""
    assert sigma_bm(0.5, 0, 900) == pytest.approx(0.5, abs=1e-9)


def test_sigma_bm_decreases_with_t():
    """σ monotonically decreases as t → T (more info accumulated)."""
    s0 = sigma_bm(0.5, 0, 900)
    s_mid = sigma_bm(0.5, 450, 900)
    s_late = sigma_bm(0.5, 800, 900)
    assert s0 > s_mid > s_late > 0


def test_sigma_bm_negative_t_clamped():
    """Negative t treated as t=0 (defensive)."""
    assert sigma_bm(0.5, -10, 900) == sigma_bm(0.5, 0, 900)


def test_sigma_bm_smaller_at_extreme_p():
    """σ smaller at p=0.18 than p=0.5 (less variance budget)."""
    assert sigma_bm(0.18, 0, 900) < sigma_bm(0.5, 0, 900)


def test_sigma_bm_window_parametrized():
    """Different window sizes give different σ at the same fractional t."""
    s_15m = sigma_bm(0.5, 450, 900)
    s_5m = sigma_bm(0.5, 150, 300)
    # At 50% of window, σ should be the same in fractional terms:
    # σ = sqrt(0.25 × 0.5) — independent of T.
    assert s_15m == pytest.approx(s_5m, abs=1e-9)


# ─── reservation_price ────────────────────────────────────────────────────


def test_reservation_price_no_inventory_returns_p_fair():
    """q=0 ⇒ r = p_fair (no skew)."""
    p = ASParams()
    assert reservation_price(0.18, 0.0, p, 60) == pytest.approx(0.18, abs=1e-9)
    assert reservation_price(0.50, 0.0, p, 450) == pytest.approx(0.50, abs=1e-9)


def test_reservation_price_long_yes_skews_down():
    """Long YES (q > 0) ⇒ r < p_fair (we want to sell YES, advertise lower)."""
    p = ASParams(gamma_inventory_risk=0.5)
    r = reservation_price(0.50, q_shares=100, params=p, t_in_window_s=60)
    assert r < 0.50


def test_reservation_price_short_yes_skews_up():
    """Short YES (q < 0) ⇒ r > p_fair."""
    p = ASParams(gamma_inventory_risk=0.5)
    r = reservation_price(0.50, q_shares=-100, params=p, t_in_window_s=60)
    assert r > 0.50


def test_reservation_price_clamped_to_unit():
    """r is clamped to (0.001, 0.999) regardless of skew magnitude."""
    p = ASParams(gamma_inventory_risk=10.0)
    r_extreme_long = reservation_price(0.50, q_shares=10_000, params=p, t_in_window_s=60)
    assert 0.001 <= r_extreme_long <= 0.999
    r_extreme_short = reservation_price(0.50, q_shares=-10_000, params=p, t_in_window_s=60)
    assert 0.001 <= r_extreme_short <= 0.999


def test_reservation_price_collapses_at_terminal():
    """At t = T, σ = 0 ⇒ r = p_fair (no inventory skew possible)."""
    p = ASParams()
    assert reservation_price(0.18, q_shares=100, params=p, t_in_window_s=900) == pytest.approx(0.18, abs=1e-9)


# ─── optimal_total_spread ─────────────────────────────────────────────────


def test_optimal_spread_decreases_with_t():
    """Spread shrinks as t → T (less inventory risk remaining).

    AS optimal spread blows up at small t (inv_risk ∝ (T-t)²/T) and is in
    practice clamped to the ceiling there. To show the analytical
    t-dependence, sample two LATE times where both lie inside the clamps.
    """
    p = ASParams(gamma_inventory_risk=0.1, spread_floor_bps=0.0, spread_ceiling_bps=20000.0)
    s_early = optimal_total_spread(0.18, k=5.0, params=p, t_in_window_s=700)
    s_late = optimal_total_spread(0.18, k=5.0, params=p, t_in_window_s=800)
    assert s_early > s_late


def test_optimal_spread_floor_enforced():
    """δ ≥ floor even when AS analytic suggests less."""
    p = ASParams(spread_floor_bps=100.0)  # 1¢ floor
    delta = optimal_total_spread(0.18, k=1000.0, params=p, t_in_window_s=899)
    assert delta >= 100.0 / 10_000.0


def test_optimal_spread_ceiling_enforced():
    """δ ≤ ceiling even when AS analytic suggests more."""
    p = ASParams(spread_ceiling_bps=200.0)  # 2¢ ceiling
    delta = optimal_total_spread(0.18, k=0.001, params=p, t_in_window_s=0)
    assert delta <= 200.0 / 10_000.0


def test_optimal_spread_disabled_intensity_when_k_zero():
    """k ≤ 0 ⇒ intensity term is 0; spread is purely inventory-risk driven.

    Use late t and large k so both the inv_risk and intensity terms stay
    well inside the clamps and the difference between k=0 and k>0 is
    observable.
    """
    p = ASParams(gamma_inventory_risk=0.5, spread_floor_bps=0.0, spread_ceiling_bps=20000.0)
    delta_no_k = optimal_total_spread(0.5, k=0.0, params=p, t_in_window_s=850)
    delta_with_k = optimal_total_spread(0.5, k=100.0, params=p, t_in_window_s=850)
    assert delta_with_k > delta_no_k


# ─── optimal_bid_ask ──────────────────────────────────────────────────────


def test_optimal_bid_ask_symmetric_at_zero_inventory():
    """q=0 ⇒ bid and ask are symmetric around p_fair."""
    p = ASParams(spread_floor_bps=100.0)  # ensure non-zero spread
    bid, ask = optimal_bid_ask(0.5, q_shares=0, k=1.0, params=p, t_in_window_s=60)
    assert (ask - 0.5) == pytest.approx(0.5 - bid, abs=1e-9)
    assert bid < 0.5 < ask


def test_optimal_bid_ask_skewed_when_long():
    """Long YES ⇒ bid and ask both shift down."""
    p = ASParams(spread_floor_bps=100.0)
    bid_neutral, ask_neutral = optimal_bid_ask(0.5, q_shares=0, k=1.0, params=p, t_in_window_s=60)
    bid_long, ask_long = optimal_bid_ask(0.5, q_shares=100, k=1.0, params=p, t_in_window_s=60)
    assert bid_long < bid_neutral
    assert ask_long < ask_neutral


def test_optimal_bid_ask_clamped_to_unit():
    p = ASParams()
    bid, ask = optimal_bid_ask(0.05, q_shares=0, k=1.0, params=p, t_in_window_s=0)
    assert 0.001 <= bid < ask <= 0.999


def test_optimal_bid_ask_no_collapse_when_degenerate():
    """If reservation skew + spread would invert bid/ask, ensure they stay
    ordered (bid < ask) by collapsing to a 1¢ spread."""
    p = ASParams(gamma_inventory_risk=100.0, spread_floor_bps=0.0)
    bid, ask = optimal_bid_ask(0.5, q_shares=10_000, k=0.001, params=p, t_in_window_s=899)
    assert bid < ask


# ─── calibrate_c_b ────────────────────────────────────────────────────────


def test_c_b_unity_when_emp_matches_theo():
    """If empirical Δp std exactly matches the BB theoretical, c_b = 1."""
    p_mid = 0.5
    T = 900
    sample_period = 60
    theo_inc = math.sqrt(p_mid * (1 - p_mid) * sample_period / T)
    # Generate increments with exactly that std (n large for stability)
    import random

    rng = random.Random(42)
    increments = [rng.gauss(0, theo_inc) for _ in range(2000)]
    c_b = calibrate_c_b(increments, p_mid, T, sample_period)
    assert c_b == pytest.approx(1.0, abs=0.1)


def test_c_b_above_one_when_jumps():
    """Heavy-tailed (jumpy) data ⇒ empirical std > theoretical ⇒ c_b > 1.

    Theoretical 1-min increment std at p=0.5, T=900 is sqrt(0.25·60/900)≈0.13.
    Use a sample with jumps an order of magnitude larger than that.
    """
    increments = [0.5, -0.5, 0.0, 0.0, 0.0, 0.6, -0.55] * 50
    c_b = calibrate_c_b(increments, p_bucket_mid=0.5, window_seconds=900)
    assert c_b > 1.0


def test_c_b_zero_on_empty_input():
    assert calibrate_c_b([], 0.5) == 0.0
    assert calibrate_c_b([0.01], 0.5) == 0.0  # n<2


def test_c_b_zero_at_p_extreme():
    """At p=0 or p=1, theoretical std is 0 ⇒ c_b returns 0 (defensive)."""
    assert calibrate_c_b([0.01, -0.01], 0.0) == 0.0
    assert calibrate_c_b([0.01, -0.01], 1.0) == 0.0


# ─── in_quoting_window ────────────────────────────────────────────────────


def test_in_quoting_window_within_band():
    p = ASParams(window_seconds=900, tau_terminal_s=60)
    assert in_quoting_window(60, p) is True
    assert in_quoting_window(450, p) is True
    assert in_quoting_window(840, p) is True


def test_in_quoting_window_too_early():
    p = ASParams(window_seconds=900, tau_terminal_s=60)
    assert in_quoting_window(30, p, entry_window_start_s=60) is False


def test_in_quoting_window_too_late():
    p = ASParams(window_seconds=900, tau_terminal_s=60)
    assert in_quoting_window(841, p) is False


# ─── in_active_zone ───────────────────────────────────────────────────────


def test_in_active_zone_inside_zone_outside_dead():
    """V1 nominee bucket [0.15, 0.20] active, dead zone [0.40, 0.60]."""
    assert in_active_zone(0.18, 0.15, 0.20, 0.40, 0.60) is True


def test_in_active_zone_outside_active_range():
    assert in_active_zone(0.10, 0.15, 0.20, 0.40, 0.60) is False
    assert in_active_zone(0.25, 0.15, 0.20, 0.40, 0.60) is False


def test_in_active_zone_in_dead_zone_blocks():
    """If active zone overlaps dead zone, dead zone wins."""
    assert in_active_zone(0.50, 0.30, 0.70, 0.40, 0.60) is False


def test_in_active_zone_parametrizable():
    """Helpers must NOT hardcode the V1 bucket — parametrize zone bounds."""
    # Step 0 v2 might re-target [0.20, 0.30]; the helper accepts that.
    assert in_active_zone(0.25, 0.20, 0.30, 0.40, 0.60) is True
    assert in_active_zone(0.25, 0.15, 0.20, 0.40, 0.60) is False
