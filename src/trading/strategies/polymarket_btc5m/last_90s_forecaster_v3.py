"""last_90s_forecaster_v3 — LightGBM at t=210 s with Binance
microstructure tail features (ADR 0011 follow-up).

Inherits the entry-window timing and gates from v2; the feature vector
is the v2 base (21 features) + 5 microstructure features
(``bm_cvd_normalized``, ``bm_taker_buy_ratio``, ``bm_trade_intensity``,
``bm_large_trade_flag``, ``bm_signed_autocorr_lag1``) appended at the
tail. See ``estrategias/en-desarrollo/last_90s_forecaster_v3.md``.

Shadow-only at creation. There is no active v3 model in
``research.models`` until promotion, and the strategy degrades cleanly
to ``SKIP("shadow_mode_no_model")`` — same pattern as v2. We
deliberately reuse v2's ``LGBRunner`` (with its ``num_feature()`` guard)
to keep the train/serve drift check identical across families.

Microstructure providers: at runtime the strategy depends on a
``MicrostructureProviderLike`` injected by the engine (Phase-3
serving wiring is not part of this session — the in-shadow path
uses sentinels and never enters, so behavior is correct without it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from trading.common.logging import get_logger
from trading.engine.features import macro as macro_feat
from trading.engine.features.binance_microstructure import (
    binance_microstructure_from_trades,
)
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.strategies.polymarket_btc5m._lgb_runner import LGBRunner
from trading.strategies.polymarket_btc5m._v2_features import (
    V2FeatureInputs,
    build_vector,
    feature_names,
)

log = get_logger("strategy.last_90s_forecaster_v3")

# Canonical v3 feature order: v2 base + 5 microstructure at tail.
_V3_TAIL: tuple[str, ...] = (
    "bm_cvd_normalized",
    "bm_taker_buy_ratio",
    "bm_trade_intensity",
    "bm_large_trade_flag",
    "bm_signed_autocorr_lag1",
)


def feature_names_v3() -> tuple[str, ...]:
    return feature_names(False) + _V3_TAIL


class MacroProviderLike(Protocol):
    def snapshot_at(self, as_of_ts: float) -> macro_feat.MacroSnapshot | None: ...


class ModelRunner(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


class MicrostructureProviderLike(Protocol):
    def fetch(self, ts: float) -> dict[str, float]: ...


class Last90sForecasterV3(StrategyBase):
    name = "last_90s_forecaster_v3"

    def __init__(
        self,
        config: dict,
        macro_provider: MacroProviderLike | None = None,
        model: ModelRunner | None = None,
        microstructure_provider: MicrostructureProviderLike | None = None,
    ) -> None:
        super().__init__(config)
        self.macro = macro_provider
        self.model = model
        self.ms_provider = microstructure_provider

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params
        entry_start = float(p.get("entry_window_start_s", 205))
        entry_end = float(p.get("entry_window_end_s", 215))
        edge_threshold = float(p.get("edge_threshold", 0.04))
        spread_max = float(p.get("spread_max_bps", 150.0))
        adx_threshold = float(p.get("adx_threshold", 20.0))
        consecutive_min = int(p.get("consecutive_min", 2))
        ms_window_s = int(p.get("microstructure_window_seconds", 90))
        large_threshold_usd = float(p.get("large_trade_threshold_usd", 100_000.0))
        shadow = bool(self.config.get("paper", {}).get("shadow", True))

        if not (entry_start <= ctx.t_in_window <= entry_end):
            return Decision(Action.SKIP, reason="outside_entry_window")

        spots = [
            t.spot_price
            for t in ctx.recent_ticks
            if hasattr(t, "ts") and (ctx.ts - t.ts) <= 90.0 and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < 60:
            return Decision(Action.SKIP, reason="insufficient_micro_data")

        macro_snap = None
        if self.macro is not None:
            macro_snap = self.macro.snapshot_at(ctx.ts)
        if macro_snap is None:
            return Decision(Action.SKIP, reason="no_macro_snapshot")

        regime = macro_feat.classify_regime(
            macro_snap.ema8,
            macro_snap.ema34,
            macro_snap.adx_14,
            macro_snap.consecutive_same_dir,
            adx_threshold=adx_threshold,
            consecutive_min=consecutive_min,
        )
        macro_snap_eff = macro_feat.MacroSnapshot(
            ema8=macro_snap.ema8,
            ema34=macro_snap.ema34,
            adx_14=macro_snap.adx_14,
            consecutive_same_dir=macro_snap.consecutive_same_dir,
            regime=regime,
            ema8_vs_ema34_pct=macro_snap.ema8_vs_ema34_pct,
        )

        inputs = V2FeatureInputs(
            as_of_ts=ctx.ts,
            spots_last_90s=spots,
            macro_snap=macro_snap_eff,
            implied_prob_yes=ctx.implied_prob_yes,
            yes_ask=ctx.pm_yes_ask,
            no_ask=ctx.pm_no_ask,
            depth_yes=ctx.pm_depth_yes,
            depth_no=ctx.pm_depth_no,
            pm_imbalance=ctx.pm_imbalance,
            pm_spread_bps=ctx.pm_spread_bps,
            open_price=ctx.open_price,
            t_in_window_s=ctx.t_in_window,
        )
        base_vec = build_vector(inputs, include_bb_residual=False)

        if self.ms_provider is not None:
            ms_features = self.ms_provider.fetch(ctx.ts)
        else:
            # Shadow boot path: the engine has not wired a sync microstructure
            # provider yet (planned in promotion sprint). Sentinels keep the
            # vector well-formed; SKIP("shadow_mode_no_model") below means
            # the model is never asked, so this never affects an entry.
            ms_features = binance_microstructure_from_trades(
                trades=[],
                baseline_trades_24h=0,
                window_s=ms_window_s,
                large_threshold_usd=large_threshold_usd,
            )
        ms_tail = [ms_features[k] for k in _V3_TAIL]
        vec = base_vec + ms_tail
        names = feature_names_v3()

        if self.model is None:
            features_dbg = dict(zip(names, vec, strict=True))
            return Decision(
                Action.SKIP,
                reason="shadow_mode_no_model",
                signal_features=features_dbg,
            )

        micro_prob = self.model.predict_proba(vec)
        edge = micro_prob - ctx.implied_prob_yes
        features = dict(zip(names, vec, strict=True))
        features.update(
            {
                "micro_prob": micro_prob,
                "edge": edge,
                "regime": regime,
                "shadow": shadow,
            }
        )

        if ctx.pm_spread_bps > spread_max:
            return Decision(Action.SKIP, reason="spread_too_wide", signal_features=features)
        if regime == "uptrend" and micro_prob <= 0.5:
            return Decision(Action.SKIP, reason="macro_contradicts_micro", signal_features=features)
        if regime == "downtrend" and micro_prob >= 0.5:
            return Decision(Action.SKIP, reason="macro_contradicts_micro", signal_features=features)
        if abs(edge) < edge_threshold:
            return Decision(Action.SKIP, reason="edge_below_threshold", signal_features=features)

        side = Side.YES_UP if edge > 0 else Side.YES_DOWN
        if shadow:
            return Decision(Action.SKIP, reason="shadow_mode", signal_features=features)

        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            signal_breakdown={"edge": edge, "micro_prob": micro_prob, "regime": regime},
            reason=f"edge={edge:+.4f} micro_prob={micro_prob:.4f} regime={regime}",
        )


async def load_runner_async(
    name: str = "last_90s_forecaster_v3",
) -> ModelRunner | None:
    """Async-native lookup. Mirrors v2's pattern; reuses v2's LGBRunner
    so the n_features guard is enforced identically. Returns None when
    no `is_active=true` row exists for ``name`` — caller boots strategy
    in shadow."""
    from trading.common.db import acquire

    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT path FROM research.models " "WHERE name = $1 AND is_active = TRUE",
                name,
            )
    except Exception as e:
        log.warning("v3.model_lookup_err", err=str(e))
        return None
    if row is None:
        log.info("v3.no_active_model_row", name=name)
        return None
    path = Path(row["path"])
    model_file = path / "model.lgb"
    calibrator_file = path / "calibrator.pkl"
    if not model_file.exists():
        log.error("v3.model_file_missing", path=str(model_file))
        return None
    try:
        return LGBRunner(model_file, calibrator_path=calibrator_file)
    except Exception as e:
        log.error("v3.model_load_err", err=str(e), path=str(model_file))
        return None


def load_runner_from_registry(
    name: str = "last_90s_forecaster_v3",
) -> ModelRunner | None:
    import asyncio

    return asyncio.run(load_runner_async(name))
