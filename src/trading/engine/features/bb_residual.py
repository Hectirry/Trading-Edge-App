"""Brownian-bridge prior for binary up/down 5 m markets (Phase 2 v2_bbres).

Given a 5 m polymarket window with strike ``open_price`` (anchor), spot
``S_t`` at time ``t`` since open, and volatility ``σ`` expressed as the
stddev of log-return *per sqrt(second)*, the no-drift Brownian-bridge
probability that ``S_T > K`` (i.e. YES resolves UP) is the linear
z-score CDF::

    z = (S_t - K) / (K · σ · sqrt(max(T - t, eps)))
    p_BM = Φ(z)

This is the geometric/no-drift prior. Subtracting it from the trained
model probability gives a *residual* that, in theory, captures any
non-Brownian structure the model has learned.

### Scale of the volatility input

``brownian_bridge_prob`` expects ``vol_per_sqrt_s`` — the stddev of
1 s log-returns. Two upstream sources expose volatility:

1. ``trading.engine.features.micro.realized_vol_yz(spots, n)`` returns
   ``sqrt(Var(Δlog spot))`` of 1 Hz log-returns — already per-sqrt(s).
   Pass it directly.
2. ``TickContext.vol_ewma`` from ``IndicatorStack`` (see
   ``trading.engine.indicators.EWMAVol``) is *annualised*:
   ``sqrt(var · 365·24·3600)``. Convert via ``vol / sqrt(31_536_000)``
   before calling ``brownian_bridge_prob``. ``bb_residual_features``
   below does this conversion when given a ``TickContext``.

Mixing scales is the classic feature-spec error that masks a useless
feature as a "weak signal" — the conversion is explicit so reviewers
can audit it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading.engine.types import TickContext


_SECONDS_PER_YEAR: float = 365.0 * 24.0 * 3600.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def brownian_bridge_prob(
    spot: float,
    open_: float,
    t_in_window_s: float,
    vol_per_sqrt_s: float,
    T: float = 300.0,
    eps: float = 1e-6,
) -> float:
    """No-drift Brownian-bridge P(S_T > open) at time ``t``.

    Defensive returns:
    - ``open_ <= 0`` → 0.5 (no anchor → no signal).
    - ``vol_per_sqrt_s <= 0`` → 0.5 (no dispersion → degenerate).
    - ``t_in_window_s >= T - eps`` → 1.0 if spot > open, 0.0 if spot < open,
      0.5 if spot == open (window already closed; outcome is determined).

    Result is always in ``[0.0, 1.0]``.
    """
    if open_ <= 0.0:
        return 0.5
    if vol_per_sqrt_s <= 0.0:
        return 0.5
    delta = spot - open_
    tau = T - t_in_window_s
    if tau <= eps:
        if delta > 0.0:
            return 1.0
        if delta < 0.0:
            return 0.0
        return 0.5
    z = delta / (open_ * vol_per_sqrt_s * math.sqrt(tau))
    return _norm_cdf(z)


def bb_residual_features(
    ctx: TickContext,
    *,
    T_seconds: float = 300.0,
    eps: float = 1e-6,
) -> dict[str, float]:
    """Four bb_residual features keyed by the canonical names.

    Reads ``ctx.spot_price``, ``ctx.open_price``, ``ctx.t_in_window``,
    ``ctx.vol_ewma`` (annualised → converted), ``ctx.implied_prob_yes``,
    ``ctx.model_prob_yes``.

    Missing-model handling: if ``ctx.model_prob_yes`` is ``None`` or
    ``NaN``, ``bb_model_vs_prior`` and ``bb_edge_vs_market`` are
    returned as ``0.0`` (deliberate finite sentinel — LightGBM trees
    can split on it, and tests assert all four values are finite).
    """
    spot = float(ctx.spot_price or 0.0)
    open_ = float(ctx.open_price or 0.0)
    t = float(ctx.t_in_window or 0.0)
    vol_annual = float(ctx.vol_ewma or 0.0)
    vol_per_sqrt_s = vol_annual / math.sqrt(_SECONDS_PER_YEAR) if vol_annual > 0.0 else 0.0
    implied = float(ctx.implied_prob_yes or 0.0)

    p_prior = brownian_bridge_prob(spot, open_, t, vol_per_sqrt_s, T=T_seconds, eps=eps)

    raw_model = ctx.model_prob_yes
    has_model = (
        raw_model is not None
        and isinstance(raw_model, int | float)
        and not math.isnan(float(raw_model))
    )
    model = float(raw_model) if has_model else None

    if model is None:
        bb_model_vs_prior = 0.0
        bb_edge_vs_market = 0.0
    else:
        bb_model_vs_prior = model - p_prior
        bb_edge_vs_market = model - implied

    bb_market_vs_prior = implied - p_prior

    return {
        "bb_p_prior": p_prior,
        "bb_model_vs_prior": bb_model_vs_prior,
        "bb_market_vs_prior": bb_market_vs_prior,
        "bb_edge_vs_market": bb_edge_vs_market,
    }
