"""Black-Scholes binary digital pricer for Polymarket BTC up/down 5 m.

Used by ``oracle_lag_v1``. Pure analytic form — no model training, no
historical lookback beyond a σ EWMA. The hypothesis driving the strategy
(brief 2026-04-26, ADR 0013) is that the closing residual:

    P(close > strike | δ, τ, σ)  =  Φ( δ / (σ √τ) )

dominates the Polymarket implied probability when τ → 0 because σ√τ
collapses faster than δ accumulates. δ is the percent move from the
window-open Chainlink reference; σ is per-second volatility (BTC quiet
regime ≈ 0.03 %/√s); τ is seconds until the next observationsTimestamp.

Edge cases: τ ≤ 0 or σ√τ ≤ 0 collapses to a step function — sign of δ
determines the outcome. We return 0.5 only when δ is exactly zero in
the degenerate case (callers should have already gated on a minimum
|δ| before this point).
"""

from __future__ import annotations

import math
from statistics import NormalDist

_NORM = NormalDist()


def p_up(delta_pct: float, tau_s: float, sigma_per_sqrt_s: float) -> float:
    """P(close > strike) under no-drift Brownian-bridge.

    delta_pct: relative move from open. ``+0.0003`` means +3 bps (0.03%).
    tau_s: seconds remaining until window close. Must be ≥ 0.
    sigma_per_sqrt_s: volatility per √second on the same percent scale.
        BTC quiet regime ~3e-4 (0.03 %/√s); stress 1e-3 (0.1 %/√s).

    Returns float in [0, 1]. Degenerate input collapses cleanly.
    """
    if tau_s <= 0 or sigma_per_sqrt_s <= 0:
        if delta_pct > 0:
            return 1.0
        if delta_pct < 0:
            return 0.0
        return 0.5
    sigma_sqrt_tau = sigma_per_sqrt_s * math.sqrt(tau_s)
    z = delta_pct / sigma_sqrt_tau
    return _NORM.cdf(z)


def sigma_ewma(spots: list[float], lam: float = 0.94) -> float:
    """EWMA volatility-per-second from a 1 Hz spot series.

    ``spots`` is chronologically ordered; each consecutive pair contributes
    one log-return r_i = ln(spots[i]/spots[i-1]). The classic RiskMetrics
    EWMA σ²_t = λ·σ²_{t-1} + (1-λ)·r²_t is applied. Returns σ on the
    per-√second scale (since the underlying tick spacing is 1 s, no
    extra rescaling is needed).

    Returns 0.0 if fewer than 2 valid spots (caller should treat that
    as "do not enter").
    """
    if len(spots) < 2:
        return 0.0
    var = 0.0
    last = None
    for s in spots:
        if s <= 0:
            continue
        if last is None:
            last = s
            continue
        r = math.log(s / last)
        var = lam * var + (1 - lam) * r * r
        last = s
    return math.sqrt(max(var, 0.0))


def tick_rule_cvd(spots: list[float]) -> float:
    """Tick-rule approximation of CVD direction from a spot series.

    Sums the signed direction (``+1`` up-tick, ``-1`` down-tick, ``0``
    flat) over consecutive pairs and divides by the number of moves.
    Returns a value in ``[-1, +1]``.

    This is a *proxy* for the real Cont-Kukanov-Stoikov 2014 OFI which
    requires taker-side flags from individual trades. The taker flag
    isn't on the strategy's hot-path tick data, so we use the price-
    movement sign instead. Less precise than real OFI on a single tick
    but directionally correct over windows ≥ 10 s in our experience.
    """
    if len(spots) < 2:
        return 0.0
    n_moves = 0
    signed_sum = 0
    for i in range(1, len(spots)):
        d = spots[i] - spots[i - 1]
        if d > 0:
            signed_sum += 1
            n_moves += 1
        elif d < 0:
            signed_sum -= 1
            n_moves += 1
        # d == 0 is neither up nor down → ignored, doesn't count.
    if n_moves == 0:
        return 0.0
    return signed_sum / n_moves


__all__ = ["p_up", "sigma_ewma", "tick_rule_cvd"]
