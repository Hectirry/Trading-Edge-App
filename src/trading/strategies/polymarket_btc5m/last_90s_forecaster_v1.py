"""last_90s_forecaster_v1 — rules baseline at t=210 s (ADR 0011).

Enters 90 s before window close using:

- micro momentum (last 90 s of 1 Hz BTC spot)
- macro regime (EMA 8 / 34 + ADX 14 + consecutive streak over the last
  20+ closed 5 m Binance candles)
- Polymarket microstructure (implied_prob_yes, spread)

Decision is a small explicit tree; every leaf names its ``reason`` so
the driver's 60 s eval summary attributes SKIPs cleanly.
"""

from __future__ import annotations

from typing import Protocol

from trading.engine.features import macro as macro_feat
from trading.engine.features import micro as micro_feat
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext


class MacroProviderLike(Protocol):
    def snapshot_at(self, as_of_ts: float) -> macro_feat.MacroSnapshot | None: ...


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Last90sForecasterV1(StrategyBase):
    name = "last_90s_forecaster_v1"

    def __init__(
        self,
        config: dict,
        macro_provider: MacroProviderLike | None = None,
    ) -> None:
        super().__init__(config)
        self.macro = macro_provider

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params

        entry_start = float(p.get("entry_window_start_s", 205))
        entry_end = float(p.get("entry_window_end_s", 215))
        divisor = float(p.get("momentum_divisor_bps", 40.0))
        edge_threshold = float(p.get("edge_threshold", 0.04))
        spread_max = float(p.get("spread_max_bps", 150.0))
        adx_threshold = float(p.get("adx_threshold", 20.0))
        consecutive_min = int(p.get("consecutive_min", 2))

        if not (entry_start <= ctx.t_in_window <= entry_end):
            return Decision(
                action=Action.SKIP,
                reason="outside_entry_window",
                signal_breakdown={"t_in_window": ctx.t_in_window},
            )

        spots = [
            t.spot_price for t in ctx.recent_ticks
            if hasattr(t, "ts") and (ctx.ts - t.ts) <= 90.0 and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < 60:
            return Decision(
                action=Action.SKIP,
                reason="insufficient_micro_data",
                signal_breakdown={"n_samples": len(spots)},
            )

        m90 = micro_feat.momentum_bps(spots, 90)
        m30 = micro_feat.momentum_bps(spots, 30)
        m60 = micro_feat.momentum_bps(spots, 60)
        rv = micro_feat.realized_vol_yz(spots, 90)
        tur = micro_feat.tick_up_ratio(spots, 90)

        macro_snap: macro_feat.MacroSnapshot | None = None
        if self.macro is not None:
            macro_snap = self.macro.snapshot_at(ctx.ts)
        if macro_snap is None:
            return Decision(
                action=Action.SKIP,
                reason="no_macro_snapshot",
                signal_breakdown={"ts": ctx.ts},
            )

        # Re-classify regime with this strategy's thresholds (the provider
        # might have been configured with different defaults).
        regime = macro_feat.classify_regime(
            macro_snap.ema8, macro_snap.ema34,
            macro_snap.adx_14, macro_snap.consecutive_same_dir,
            adx_threshold=adx_threshold, consecutive_min=consecutive_min,
        )

        micro_prob = 0.5 + _clamp(m90 / divisor, -0.45, 0.45)
        edge = micro_prob - ctx.implied_prob_yes

        features = {
            "m30_bps": m30,
            "m60_bps": m60,
            "m90_bps": m90,
            "rv_90s": rv,
            "tick_up_ratio": tur,
            "micro_prob": micro_prob,
            "edge": edge,
            "regime": regime,
            "ema8": macro_snap.ema8,
            "ema34": macro_snap.ema34,
            "adx_14": macro_snap.adx_14,
            "consecutive_same_dir": macro_snap.consecutive_same_dir,
            "implied_prob_yes": ctx.implied_prob_yes,
            "pm_spread_bps": ctx.pm_spread_bps,
        }

        if ctx.pm_spread_bps > spread_max:
            return Decision(
                action=Action.SKIP, reason="spread_too_wide",
                signal_features=features,
                signal_breakdown={"pm_spread_bps": ctx.pm_spread_bps, "max": spread_max},
            )

        if regime == "uptrend" and micro_prob <= 0.5:
            return Decision(
                action=Action.SKIP, reason="macro_contradicts_micro",
                signal_features=features,
                signal_breakdown={"regime": regime, "micro_prob": micro_prob},
            )
        if regime == "downtrend" and micro_prob >= 0.5:
            return Decision(
                action=Action.SKIP, reason="macro_contradicts_micro",
                signal_features=features,
                signal_breakdown={"regime": regime, "micro_prob": micro_prob},
            )

        if abs(edge) < edge_threshold:
            return Decision(
                action=Action.SKIP, reason="edge_below_threshold",
                signal_features=features,
                signal_breakdown={"edge": edge, "threshold": edge_threshold},
            )

        side = Side.YES_UP if edge > 0 else Side.YES_DOWN
        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            signal_breakdown={
                "edge": edge,
                "micro_prob": micro_prob,
                "regime": regime,
            },
            reason=f"edge={edge:+.4f} micro_prob={micro_prob:.4f} regime={regime}",
        )
