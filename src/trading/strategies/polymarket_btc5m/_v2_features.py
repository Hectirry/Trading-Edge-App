"""Feature builder for last_90s_forecaster_v2 (ADR 0011).

Single source of truth for the feature vector. Imported by both the
training CLI and the runtime strategy so they are guaranteed
bit-for-bit identical.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from trading.engine.features import macro as macro_feat
from trading.engine.features import micro as micro_feat
from trading.engine.features.jumps import lee_mykland_jump_flag
from trading.engine.features.microprice import microprice

# Canonical feature order. Keep in sync with meta.json.
FEATURE_NAMES: tuple[str, ...] = (
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


@dataclass(frozen=True)
class V2FeatureInputs:
    """Everything ``build_vector`` needs — all derivable from paper_ticks +
    crypto_ohlcv + the tick-context snapshot the driver already assembles.
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


def build_vector(inp: V2FeatureInputs) -> list[float]:
    """Return feature vector ordered by ``FEATURE_NAMES``."""
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

    return [
        m30, m60, m90, rv, tur,
        inp.macro_snap.ema8_vs_ema34_pct,
        inp.macro_snap.adx_14,
        float(inp.macro_snap.consecutive_same_dir),
        regime_up, regime_dn, regime_rn,
        inp.implied_prob_yes,
        mp,
        mp - inp.implied_prob_yes,
        inp.pm_imbalance,
        inp.pm_spread_bps,
        lm,
        hour_sin, hour_cos, dow_sin, dow_cos,
    ]
