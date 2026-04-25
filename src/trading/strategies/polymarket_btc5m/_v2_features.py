"""Feature builder for last_90s_forecaster_v2 (ADR 0011).

Single source of truth for the feature vector. Imported by both the
training CLI and the runtime strategy so they are guaranteed
bit-for-bit identical.

The 4 ``bb_*`` features at the tail are appended (not interleaved) so
existing trained models keep loading against the unchanged 21-feature
prefix; new training runs that pass ``include_bb_residual=True`` produce
25-feature models. See ``estrategias/en-desarrollo/last_90s_forecaster_v2_bbres.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from trading.engine.features import macro as macro_feat
from trading.engine.features import micro as micro_feat
from trading.engine.features.bb_residual import brownian_bridge_prob
from trading.engine.features.jumps import lee_mykland_jump_flag
from trading.engine.features.microprice import microprice

# Canonical feature order. Keep in sync with meta.json.
# bb_* are appended at the END so models trained without them still
# load and serve correctly against the 21-feature prefix.
_FEATURE_NAMES_BASE: tuple[str, ...] = (
    "m30_bps",
    "m60_bps",
    "m90_bps",
    "rv_90s",
    "tick_up_ratio",
    "ema8_vs_ema34_pct",
    "adx_14",
    "consecutive_same_dir",
    "regime_uptrend",
    "regime_downtrend",
    "regime_range",
    "implied_prob_yes",
    "microprice",
    "microprice_minus_implied",
    "pm_imbalance",
    "pm_spread_bps",
    "lee_mykland_jump_60s",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
)
_FEATURE_NAMES_BB: tuple[str, ...] = (
    "bb_p_prior",
    "bb_model_vs_prior",
    "bb_market_vs_prior",
    "bb_edge_vs_market",
)
FEATURE_NAMES: tuple[str, ...] = _FEATURE_NAMES_BASE + _FEATURE_NAMES_BB


def feature_names(include_bb_residual: bool) -> tuple[str, ...]:
    return FEATURE_NAMES if include_bb_residual else _FEATURE_NAMES_BASE


@dataclass(frozen=True)
class V2FeatureInputs:
    """Everything ``build_vector`` needs — all derivable from paper_ticks +
    crypto_ohlcv + the tick-context snapshot the driver already assembles.

    The ``open_price`` / ``t_in_window_s`` / ``bb_T_seconds`` fields are
    only consumed when ``include_bb_residual`` is set on the build call;
    they default to neutral values so legacy callers (training paths that
    don't set them) still work without bb_* features.
    """

    as_of_ts: float
    spots_last_90s: list[float]
    macro_snap: macro_feat.MacroSnapshot
    implied_prob_yes: float
    yes_ask: float
    no_ask: float
    depth_yes: float
    depth_no: float
    pm_imbalance: float
    pm_spread_bps: float
    # bb_residual inputs. Defaults give neutral / 0.5 prior so callers
    # that don't populate them still get safe values when bb is enabled.
    open_price: float = 0.0
    t_in_window_s: float = 210.0
    bb_T_seconds: float = 300.0


def build_vector(inp: V2FeatureInputs, *, include_bb_residual: bool = False) -> list[float]:
    """Return feature vector ordered by ``FEATURE_NAMES`` (or its base
    21-feature prefix when ``include_bb_residual=False``)."""
    m30 = micro_feat.momentum_bps(inp.spots_last_90s, 30)
    m60 = micro_feat.momentum_bps(inp.spots_last_90s, 60)
    m90 = micro_feat.momentum_bps(inp.spots_last_90s, 90)
    rv = micro_feat.realized_vol_yz(inp.spots_last_90s, 90)
    tur = micro_feat.tick_up_ratio(inp.spots_last_90s, 90)

    mp = microprice(inp.yes_ask, inp.no_ask, inp.depth_yes, inp.depth_no)

    regime_up = 1.0 if inp.macro_snap.regime == "uptrend" else 0.0
    regime_dn = 1.0 if inp.macro_snap.regime == "downtrend" else 0.0
    regime_rn = 1.0 if inp.macro_snap.regime == "range" else 0.0

    lm = 1.0 if lee_mykland_jump_flag(inp.spots_last_90s, window_s=60) else 0.0

    dt = datetime.fromtimestamp(inp.as_of_ts, tz=UTC)
    hour = dt.hour + dt.minute / 60.0
    hour_sin = math.sin(2 * math.pi * hour / 24.0)
    hour_cos = math.cos(2 * math.pi * hour / 24.0)
    dow = dt.weekday()
    dow_sin = math.sin(2 * math.pi * dow / 7.0)
    dow_cos = math.cos(2 * math.pi * dow / 7.0)

    base = [
        m30,
        m60,
        m90,
        rv,
        tur,
        inp.macro_snap.ema8_vs_ema34_pct,
        inp.macro_snap.adx_14,
        float(inp.macro_snap.consecutive_same_dir),
        regime_up,
        regime_dn,
        regime_rn,
        inp.implied_prob_yes,
        mp,
        mp - inp.implied_prob_yes,
        inp.pm_imbalance,
        inp.pm_spread_bps,
        lm,
        hour_sin,
        hour_cos,
        dow_sin,
        dow_cos,
    ]
    if not include_bb_residual:
        return base

    # bb_residual block — appended at the tail. ``rv`` from realized_vol_yz
    # is the stddev of 1 s log-returns (per-sqrt-s), exactly what
    # ``brownian_bridge_prob`` expects.
    #
    # Honesty caveat: ``model_prob_yes`` in the original spec referenced
    # ``ctx.model_prob_yes`` (IndicatorStack's annualised-vol BS prob), but
    # (a) it is not available in training where there is no IndicatorStack,
    # and (b) using a different vol estimate at serving than at training
    # introduces a silent train/serve skew. We therefore set
    # ``model_prob = p_prior`` in both paths, which makes
    # ``bb_model_vs_prior ≡ 0`` and ``bb_edge_vs_market ≡ -bb_market_vs_prior``.
    # The two redundant features are kept in the vector so the column
    # order matches the spec; LightGBM splits will collapse them.
    # ``bb_p_prior`` and ``bb_market_vs_prior`` are the only features
    # carrying real new signal — the second is the lead-lag of Polymarket
    # vs the no-drift Brownian-bridge prior on Binance spot.
    p_prior = brownian_bridge_prob(
        spot=inp.spots_last_90s[-1] if inp.spots_last_90s else 0.0,
        open_=inp.open_price,
        t_in_window_s=inp.t_in_window_s,
        vol_per_sqrt_s=rv,
        T=inp.bb_T_seconds,
    )
    model_prob = p_prior
    bb_model_vs_prior = model_prob - p_prior
    bb_market_vs_prior = inp.implied_prob_yes - p_prior
    bb_edge_vs_market = model_prob - inp.implied_prob_yes
    return base + [p_prior, bb_model_vs_prior, bb_market_vs_prior, bb_edge_vs_market]
