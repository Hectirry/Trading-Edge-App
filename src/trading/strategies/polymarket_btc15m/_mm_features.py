"""Avellaneda-Stoikov primitives adapted to Polymarket binary markets (mm_rebate_v1).

This module is **helpers only** (Step 1). The strategy class is Step 2.

Core formulas
-------------
For a binary market settling at horizon T to {0, 1}:

  σ_BM(t)   = sqrt( p_fair · (1 − p_fair) · (T − t) / T )         # bridge std
  r(t, q)   = p_fair − q · γ · σ_BM(t)² · (T − t)                  # reservation price
  δ_total   = γ · σ_BM(t)² · (T − t) + (2/γ) · ln(1 + γ/k)         # AS optimal spread
  δ_bid     = δ_total / 2 + (r − p_fair)
  δ_ask     = δ_total / 2 − (r − p_fair)

Units: prices in YES-units (∈ [0, 1]); time in seconds; q in shares (signed,
long YES = +q, long NO = −q).

What is parametrizable (NOT hardcoded for any one bucket)
---------------------------------------------------------
- Bucket boundaries pasados como argumento (no hardcoded a 0.15-0.20).
- γ (gamma_inventory_risk) is per-strategy config.
- T (window_seconds) is per-strategy config; default 900 (15-min) but other
  horizons reuse the same math.

Step 0 v2 dependencies (TODO post-paper_ticks-15m)
--------------------------------------------------
- ``calibrate_c_b``: empirical std currently uses 1-min Δp from
  polymarket_prices_history. Once paper_ticks 15m has 1Hz mid data, switch
  to sub-minute realized vol for higher fidelity.
- ``optimal_bid_ask``: assumes symmetric bid/ask spread around mid. With
  paper_ticks 15m bid/ask we can validate or replace with real best-bid /
  best-ask anchoring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ASParams:
    """Avellaneda-Stoikov tunable parameters.

    All knobs come from `[params]` in the strategy TOML. Frozen so the
    strategy can pass an `ASParams` around without mutation surprises.
    """

    gamma_inventory_risk: float = 0.5  # γ — risk aversion to inventory
    window_seconds: int = 900           # T — 15-min binary market by default
    spread_floor_bps: float = 50.0      # min total spread (50bps = 0.5¢)
    spread_ceiling_bps: float = 500.0   # max total spread (safety cap)
    tau_terminal_s: float = 60.0        # do not quote in last τ_terminal seconds


def sigma_bm(p_fair: float, t_in_window_s: float, window_seconds: int = 900) -> float:
    """Brownian-bridge instantaneous std at time t.

    σ_BM(t) = sqrt( p · (1 − p) · (T − t) / T )

    Boundary cases:
      - t < 0  → clamp to 0   (treat as t=0; full uncertainty)
      - t > T  → return 0     (resolved; no remaining variance)
      - p ∉ (0, 1) → return 0 (terminal-degenerate prior; nothing to do)
    """
    if not (0.0 < p_fair < 1.0):
        return 0.0
    if t_in_window_s >= window_seconds:
        return 0.0
    t = max(0.0, t_in_window_s)
    return math.sqrt(p_fair * (1.0 - p_fair) * (window_seconds - t) / window_seconds)


def reservation_price(
    p_fair: float,
    q_shares: float,
    params: ASParams,
    t_in_window_s: float,
) -> float:
    """AS reservation price for a maker holding signed inventory q.

    r(t, q) = p_fair − q · γ · σ²(t) · (T − t)

    Returns p_fair when q == 0 (no inventory skew). Clamped to (0, 1) since
    the binary support is bounded.
    """
    sigma = sigma_bm(p_fair, t_in_window_s, params.window_seconds)
    if sigma == 0.0:
        return p_fair
    t_remaining = max(0.0, params.window_seconds - t_in_window_s)
    skew = q_shares * params.gamma_inventory_risk * (sigma ** 2) * t_remaining
    r = p_fair - skew
    return min(0.999, max(0.001, r))


def optimal_total_spread(
    p_fair: float,
    k: float,
    params: ASParams,
    t_in_window_s: float,
) -> float:
    """AS optimal total spread (sum of bid-side + ask-side distances from mid).

    δ_total = γ · σ²(t) · (T − t) + (2/γ) · ln(1 + γ/k)

    `k` is the per-bucket fill intensity (fills/minute at our quoting δ),
    estimated by the k_estimator. If k <= 0, the second term is disabled
    (no fill-intensity contribution; spread is purely inventory-risk driven).

    Returned as YES-price units. Clamped to [floor, ceiling] from params.
    """
    sigma = sigma_bm(p_fair, t_in_window_s, params.window_seconds)
    t_remaining = max(0.0, params.window_seconds - t_in_window_s)
    inv_risk_term = params.gamma_inventory_risk * (sigma ** 2) * t_remaining
    intensity_term = 0.0
    if k > 0 and params.gamma_inventory_risk > 0:
        intensity_term = (2.0 / params.gamma_inventory_risk) * math.log(
            1.0 + params.gamma_inventory_risk / k
        )
    delta = inv_risk_term + intensity_term
    floor = params.spread_floor_bps / 10_000.0
    ceiling = params.spread_ceiling_bps / 10_000.0
    return min(ceiling, max(floor, delta))


def optimal_bid_ask(
    p_fair: float,
    q_shares: float,
    k: float,
    params: ASParams,
    t_in_window_s: float,
) -> tuple[float, float]:
    """Return (bid_price, ask_price) for a maker quoting around p_fair.

    bid = r − δ_total/2,  ask = r + δ_total/2

    where r is the reservation price (skewed by inventory). Both clamped
    to (0, 1).

    NOTE for Step 0 v2: this function assumes symmetric bid/ask placement
    around r. With paper_ticks 15m we can validate against observed
    best-bid/best-ask and consider asymmetric placement when one side is
    unmatched.
    """
    r = reservation_price(p_fair, q_shares, params, t_in_window_s)
    delta_total = optimal_total_spread(p_fair, k, params, t_in_window_s)
    half = delta_total / 2.0
    bid = max(0.001, min(0.999, r - half))
    ask = max(0.001, min(0.999, r + half))
    if bid >= ask:
        # Degenerate case — collapse to a 1¢ spread around r.
        bid = max(0.001, r - 0.005)
        ask = min(0.999, r + 0.005)
    return bid, ask


def calibrate_c_b(
    price_increments: list[float],
    p_bucket_mid: float,
    window_seconds: int = 900,
    sample_period_s: float = 60.0,
) -> float:
    """Compute c_b = std(empirical Δp) / std_theoretical(BB).

    Used to detect when the Brownian-bridge model under- or over-states the
    realized volatility per bucket. c_b ≈ 1 means the model fits; c_b > 2
    means jumps / regime are dominating and we should warn.

    Inputs:
      - price_increments: per-sample Δp_yes within the bucket
      - p_bucket_mid: midpoint of the bucket for the theoretical std
      - sample_period_s: spacing between consecutive samples
    """
    if not price_increments:
        return 0.0
    n = len(price_increments)
    if n < 2:
        return 0.0
    mean = sum(price_increments) / n
    var = sum((x - mean) ** 2 for x in price_increments) / (n - 1)
    emp_std = math.sqrt(var)
    # Theoretical 1-step bridge increment std at p_mid:
    #   σ_inc = sqrt(p (1-p) · sample_period / T)
    theo_std = math.sqrt(
        p_bucket_mid * (1.0 - p_bucket_mid) * sample_period_s / window_seconds
    )
    if theo_std <= 0:
        return 0.0
    return emp_std / theo_std


def in_quoting_window(
    t_in_window_s: float,
    params: ASParams,
    entry_window_start_s: float = 60.0,
) -> bool:
    """Whether the current t is inside the quoting window.

    Quote starts at `entry_window_start_s` (default 60s — let the book
    accumulate liquidity first) and ends at `T − τ_terminal` (default 60s
    cutoff before close to avoid quoting across the resolution).
    """
    if t_in_window_s < entry_window_start_s:
        return False
    if t_in_window_s > (params.window_seconds - params.tau_terminal_s):
        return False
    return True


def in_active_zone(
    p_fair: float,
    zone_lo: float,
    zone_hi: float,
    dead_zone_lo: float,
    dead_zone_hi: float,
) -> bool:
    """Whether p_fair is in the configured quoting zone AND outside the dead
    zone. Both ranges are parametrizable (NOT hardcoded to 0.15-0.20).

    For mm_rebate_v1 V1, the bucket nominee is [0.15, 0.20] but the helpers
    accept any [zone_lo, zone_hi] so Step 0 v2 can re-target without code
    changes.
    """
    if not (zone_lo <= p_fair <= zone_hi):
        return False
    if dead_zone_lo <= p_fair <= dead_zone_hi:
        return False
    return True
