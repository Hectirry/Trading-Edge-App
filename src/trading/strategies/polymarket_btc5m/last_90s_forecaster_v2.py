"""last_90s_forecaster_v2 — LightGBM at t=210 s (ADR 0011).

Same timing + decision gates as v1. Only difference: ``micro_prob_up``
is the model's predicted probability instead of the rules-based clamp.

Shadow mode (``[paper].shadow = true`` in TOML, or no active model row
in ``research.models``): evaluates everything, logs features + probs,
but emits SKIP ``shadow_mode`` instead of ENTER.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from trading.common.logging import get_logger
from trading.engine.features import macro as macro_feat
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.strategies.polymarket_btc5m._v2_features import (
    FEATURE_NAMES,
    V2FeatureInputs,
    build_vector,
)

log = get_logger("strategy.last_90s_forecaster_v2")


class MacroProviderLike(Protocol):
    def snapshot_at(self, as_of_ts: float) -> macro_feat.MacroSnapshot | None: ...


class ModelRunner(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


class LGBRunner:
    """Thin wrapper around ``lightgbm.Booster`` with optional isotonic
    calibrator. Raises loudly if lightgbm isn't installed and the runner
    is actually exercised at runtime.
    """

    def __init__(self, model_path: Path, calibrator_path: Path | None = None) -> None:
        import lightgbm as lgb  # lazy import so tests w/o lightgbm still import module

        self.booster = lgb.Booster(model_file=str(model_path))
        self._calibrator = None
        if calibrator_path is not None and calibrator_path.exists():
            import pickle

            with open(calibrator_path, "rb") as f:
                self._calibrator = pickle.load(f)

    def predict_proba(self, x: list[float]) -> float:
        import numpy as np

        arr = np.asarray([x], dtype=np.float64)
        p = float(self.booster.predict(arr)[0])
        if self._calibrator is not None:
            p = float(self._calibrator.predict([p])[0])
        return max(0.0, min(1.0, p))


class Last90sForecasterV2(StrategyBase):
    name = "last_90s_forecaster_v2"

    def __init__(
        self,
        config: dict,
        macro_provider: MacroProviderLike | None = None,
        model: ModelRunner | None = None,
    ) -> None:
        super().__init__(config)
        self.macro = macro_provider
        self.model = model

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params
        entry_start = float(p.get("entry_window_start_s", 205))
        entry_end = float(p.get("entry_window_end_s", 215))
        edge_threshold = float(p.get("edge_threshold", 0.04))
        spread_max = float(p.get("spread_max_bps", 150.0))
        adx_threshold = float(p.get("adx_threshold", 20.0))
        consecutive_min = int(p.get("consecutive_min", 2))
        shadow = bool(self.config.get("paper", {}).get("shadow", False))

        if not (entry_start <= ctx.t_in_window <= entry_end):
            return Decision(Action.SKIP, reason="outside_entry_window")

        spots = [
            t.spot_price for t in ctx.recent_ticks
            if hasattr(t, "ts") and (ctx.ts - t.ts) <= 90.0 and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < 60:
            return Decision(Action.SKIP, reason="insufficient_micro_data",
                            signal_breakdown={"n_samples": len(spots)})

        macro_snap = None
        if self.macro is not None:
            macro_snap = self.macro.snapshot_at(ctx.ts)
        if macro_snap is None:
            return Decision(Action.SKIP, reason="no_macro_snapshot")

        regime = macro_feat.classify_regime(
            macro_snap.ema8, macro_snap.ema34,
            macro_snap.adx_14, macro_snap.consecutive_same_dir,
            adx_threshold=adx_threshold, consecutive_min=consecutive_min,
        )
        macro_snap_effective = macro_feat.MacroSnapshot(
            ema8=macro_snap.ema8, ema34=macro_snap.ema34,
            adx_14=macro_snap.adx_14,
            consecutive_same_dir=macro_snap.consecutive_same_dir,
            regime=regime,
            ema8_vs_ema34_pct=macro_snap.ema8_vs_ema34_pct,
        )

        inputs = V2FeatureInputs(
            as_of_ts=ctx.ts,
            spots_last_90s=spots,
            macro_snap=macro_snap_effective,
            implied_prob_yes=ctx.implied_prob_yes,
            yes_ask=ctx.pm_yes_ask, no_ask=ctx.pm_no_ask,
            depth_yes=ctx.pm_depth_yes, depth_no=ctx.pm_depth_no,
            pm_imbalance=ctx.pm_imbalance,
            pm_spread_bps=ctx.pm_spread_bps,
        )
        vec = build_vector(inputs)

        if self.model is None:
            features_dbg = dict(zip(FEATURE_NAMES, vec, strict=True))
            return Decision(
                Action.SKIP, reason="shadow_mode_no_model",
                signal_features=features_dbg,
            )

        micro_prob = self.model.predict_proba(vec)
        edge = micro_prob - ctx.implied_prob_yes

        features = dict(zip(FEATURE_NAMES, vec, strict=True))
        features.update({
            "micro_prob": micro_prob,
            "edge": edge,
            "regime": regime,
            "shadow": shadow,
        })

        if ctx.pm_spread_bps > spread_max:
            return Decision(Action.SKIP, reason="spread_too_wide",
                            signal_features=features)

        if regime == "uptrend" and micro_prob <= 0.5:
            return Decision(Action.SKIP, reason="macro_contradicts_micro",
                            signal_features=features)
        if regime == "downtrend" and micro_prob >= 0.5:
            return Decision(Action.SKIP, reason="macro_contradicts_micro",
                            signal_features=features)

        if abs(edge) < edge_threshold:
            return Decision(Action.SKIP, reason="edge_below_threshold",
                            signal_features=features)

        side = Side.YES_UP if edge > 0 else Side.YES_DOWN

        if shadow:
            return Decision(Action.SKIP, reason="shadow_mode",
                            signal_features=features)

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


async def load_runner_async(
    name: str = "last_90s_forecaster_v2",
) -> ModelRunner | None:
    """Async-native variant for callers that live inside an event loop
    (e.g. the paper engine's main_async bootstrap).
    """
    from trading.common.db import acquire

    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT path FROM research.models "
                "WHERE name = $1 AND is_active = TRUE",
                name,
            )
    except Exception as e:
        log.warning("v2.model_lookup_err", err=str(e))
        return None
    if row is None:
        log.info("v2.no_active_model_row", name=name)
        return None
    path = Path(row["path"])
    model_file = path / "model.lgb"
    calibrator_file = path / "calibrator.pkl"
    if not model_file.exists():
        log.error("v2.model_file_missing", path=str(model_file))
        return None
    try:
        return LGBRunner(model_file, calibrator_path=calibrator_file)
    except Exception as e:
        log.error("v2.model_load_err", err=str(e), path=str(model_file))
        return None


def load_runner_from_registry(
    name: str = "last_90s_forecaster_v2",
) -> ModelRunner | None:
    """Synchronous wrapper for CLI / scripts run outside an event loop."""
    import asyncio

    return asyncio.run(load_runner_async(name))
