"""Feature builder for ``bb_residual_ofi_v1``.

Single source of truth for the 14-column feature vector. Imported by
both the runtime strategy and the training CLI so they are guaranteed
bit-for-bit identical — same pattern as ``_v2_features.py`` for
``last_90s_forecaster_v2``.

The ``FEATURE_NAMES`` order matches what
``LGBRunner.predict_proba`` expects; the
``num_feature()`` guard (reused from
``last_90s_forecaster_v2``) enforces it loud-fast on serving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FEATURE_NAMES: tuple[str, ...] = (
    "bb_p_prior",
    "bb_delta_norm",
    "ofi_composite",
    "bm_taker_buy_ratio",
    "bm_trade_intensity",
    "bm_large_trade_flag",
    "bm_signed_autocorr_lag1",
    "implied_prob_yes",
    "pm_spread_bps",
    "pm_imbalance",
    "t_in_window_s",
    "vol_per_sqrt_s",
    "fee_at_market",
    "alpha_shrinkage",
)


@dataclass(frozen=True)
class BBOFIFeatureInputs:
    """Everything ``build_vector`` needs. All values are derivable from
    the engine TickContext + a microstructure provider; the training CLI
    populates them from Postgres + the SQLite tick stores."""

    spot_price: float
    open_price: float
    t_in_window_s: float
    spots_last_90s: list[float]
    implied_prob_yes: float
    pm_spread_bps: float
    pm_imbalance: float
    # Output of ``binance_microstructure_from_trades`` (or sentinel dict
    # from ``_empty_features``). The 5 keys are required.
    ms_features: dict[str, float] = field(default_factory=dict)
    bb_T_seconds: float = 300.0
    fee_k: float = 0.0315
    alpha_min: float = 0.4
    alpha_max: float = 0.85
    alpha_ofi_gain: float = 1.0
    alpha_large_trade_bonus: float = 0.1
    entry_window_start_s: float = 60.0
    entry_window_end_s: float = 290.0


def convex_fee(p_market: float, fee_k: float) -> float:
    """Spec fee model: ``fee(p) = fee_k · 4·p·(1-p)`` — convex, peaks
    at p=0.5 (=fee_k), zero at the corners."""
    p = max(0.0, min(1.0, p_market))
    return fee_k * 4.0 * p * (1.0 - p)


def alpha_shrinkage(
    *,
    ofi_abs: float,
    large_trade_flag: float,
    t_in_window_s: float,
    entry_start_s: float,
    entry_end_s: float,
    alpha_min: float,
    alpha_max: float,
    ofi_gain: float,
    large_trade_bonus: float,
) -> float:
    """Heuristic that scales the model weight up as the OFI signal
    accumulates. Linear in |OFI|, +bonus on a large-trade event, and a
    small linear ramp with t_in_window so early-window ticks lean on
    the BB prior. Clamped to [alpha_min, alpha_max].

    Replace with ensemble-variance-driven α once the ensemble is
    trained — see strategy docstring for why this is rules-based today.
    """
    span = max(1.0, entry_end_s - entry_start_s)
    t_norm = max(0.0, min(1.0, (t_in_window_s - entry_start_s) / span))
    base = alpha_min + (alpha_max - alpha_min) * 0.5 * t_norm
    bonus = ofi_gain * min(1.0, ofi_abs) + large_trade_bonus * large_trade_flag
    return float(max(alpha_min, min(alpha_max, base + bonus)))


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
    """No-drift Brownian-bridge P(S_T > open) at time t. Defensive
    returns mirror ``trading.engine.features.bb_residual``."""
    if open_ <= 0.0 or vol_per_sqrt_s <= 0.0:
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


def realized_vol_per_sqrt_s(spots: list[float]) -> float:
    """σ of 1 Hz log-returns on the 90 s tail. Returns 0.0 when the
    series is too short or constant."""
    rets = [
        math.log(spots[i] / spots[i - 1])
        for i in range(1, len(spots))
        if spots[i - 1] > 0 and spots[i] > 0
    ]
    if len(rets) < 30:
        return 0.0
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / len(rets)
    return math.sqrt(max(var, 0.0))


def build_vector(inp: BBOFIFeatureInputs) -> tuple[list[float], dict[str, float]]:
    """Compute the 14-column vector + a debug dict of intermediates.

    Returns ``(vec, debug)`` where ``vec`` is ordered by
    ``FEATURE_NAMES`` and ``debug`` carries the same values keyed by
    name plus a couple of derived quantities the strategy uses outside
    the model input (``vol_per_sqrt_s`` for the BB step, etc.).
    """
    vol_per_sqrt_s = realized_vol_per_sqrt_s(inp.spots_last_90s)

    p_bm = brownian_bridge_prob(
        spot=inp.spot_price,
        open_=inp.open_price,
        t_in_window_s=inp.t_in_window_s,
        vol_per_sqrt_s=vol_per_sqrt_s,
        T=inp.bb_T_seconds,
    )

    # Normalised window delta — z-score of the BM bridge. Logged and
    # included in the vector but the model can drop it via splits.
    if inp.open_price > 0 and vol_per_sqrt_s > 0:
        denom = (
            inp.open_price
            * vol_per_sqrt_s
            * math.sqrt(max(inp.bb_T_seconds - inp.t_in_window_s, 1e-6))
        )
        delta_norm = (inp.spot_price - inp.open_price) / denom if denom > 0 else 0.0
    else:
        delta_norm = 0.0

    ms = inp.ms_features
    ofi_composite = float(ms.get("bm_cvd_normalized", 0.0))
    taker_buy = float(ms.get("bm_taker_buy_ratio", 0.5))
    intensity = float(ms.get("bm_trade_intensity", 1.0))
    large_trade = float(ms.get("bm_large_trade_flag", 0.0))
    signed_autocorr = float(ms.get("bm_signed_autocorr_lag1", 0.0))

    alpha = alpha_shrinkage(
        ofi_abs=abs(ofi_composite),
        large_trade_flag=large_trade,
        t_in_window_s=inp.t_in_window_s,
        entry_start_s=inp.entry_window_start_s,
        entry_end_s=inp.entry_window_end_s,
        alpha_min=inp.alpha_min,
        alpha_max=inp.alpha_max,
        ofi_gain=inp.alpha_ofi_gain,
        large_trade_bonus=inp.alpha_large_trade_bonus,
    )

    fee = convex_fee(inp.implied_prob_yes, inp.fee_k)

    vec = [
        p_bm,
        delta_norm,
        ofi_composite,
        taker_buy,
        intensity,
        large_trade,
        signed_autocorr,
        float(inp.implied_prob_yes),
        float(inp.pm_spread_bps),
        float(inp.pm_imbalance),
        float(inp.t_in_window_s),
        vol_per_sqrt_s,
        fee,
        alpha,
    ]
    debug = dict(zip(FEATURE_NAMES, vec, strict=True))
    return vec, debug
