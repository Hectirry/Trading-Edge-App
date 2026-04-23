"""contest_ensemble_v1 — ML ensemble + conformal abstention (ADR 0012).

Multi-checkpoint: evaluates at ``t_in_window ∈ {60, 120, 180, 210, 240,
270}``. At each checkpoint builds a feature vector (micro + macro +
HMM regime + candle patterns + L3 predictor outputs), passes it
through a LightGBM meta-combiner, applies conformal abstention, and
returns ENTER iff conformal decides ``predict_up`` / ``predict_down``.
If all checkpoints abstain → SKIP ``contest_abstained``.

If the HMM reports ``high_vol`` at any checkpoint → SKIP immediately
(consistent with ADR 0012 design constraint).

This v1 intentionally ships **without** the LightGBM meta-combiner
loaded from disk (Model D BiLSTM is also deferred). The strategy
falls back to an explicit rule-based ensemble of L3 predictors + L1
posterior reweighting until a model row is promoted in
``research.models`` (training CLI will be run manually when the paper
dataset grows past 3 k samples). In fallback mode the strategy still
obeys the conformal gate and skip semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from trading.engine.features import (
    hmm_regime as hmm,
)
from trading.engine.features import (
    macro as macro_feat,
)
from trading.engine.features import (
    micro as micro_feat,
)
from trading.engine.features.conformal import IsotonicConformal
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext

CHECKPOINTS = (60.0, 120.0, 180.0, 210.0, 240.0, 270.0)
CHECKPOINT_TOL_S = 3.0


class MacroProviderLike(Protocol):
    def snapshot_at(self, as_of_ts: float) -> macro_feat.MacroSnapshot | None: ...


class HMMProviderLike(Protocol):
    def predict(self, closes): ...


class MetaModelLike(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


def _nearest_checkpoint(t_in_window: float) -> float | None:
    for cp_t in CHECKPOINTS:
        if abs(t_in_window - cp_t) <= CHECKPOINT_TOL_S:
            return cp_t
    return None


class ContestEnsembleV1(StrategyBase):
    name = "contest_ensemble_v1"

    def __init__(
        self,
        config: dict,
        *,
        macro_provider: MacroProviderLike | None = None,
        hmm_detector: HMMProviderLike | None = None,
        meta_model: MetaModelLike | None = None,
        conformal: IsotonicConformal | None = None,
    ) -> None:
        super().__init__(config)
        self.macro = macro_provider
        self.hmm = hmm_detector or hmm.NullHMMRegimeDetector()
        self.model = meta_model
        self.conformal = conformal or IsotonicConformal(alpha=0.25)
        self._last_entered_per_market: dict[str, bool] = {}

    # -------- L3 predictors (compact, deterministic, rule-based) --------

    @staticmethod
    def _model_a_ofi(ctx: TickContext) -> tuple[float, float]:
        """Proxy multi-level OFI using available tick fields.

        TEA's tick recorder surfaces top-of-book only, so we derive a
        single-level OFI-like proxy: sign of (pm_imbalance - 0).
        Confidence is |imbalance| clipped to 1.
        """
        imb = float(ctx.pm_imbalance or 0.0)
        prob = 0.5 + 0.4 * max(-1.0, min(1.0, imb))
        return prob, min(1.0, abs(imb))

    @staticmethod
    def _model_b_microprice(ctx: TickContext) -> tuple[float, float]:
        # Simple drift of mid vs pm_imbalance-weighted.
        mid = 0.5 * (ctx.pm_yes_ask + (1.0 - ctx.pm_no_ask))
        weight = (ctx.pm_depth_no - ctx.pm_depth_yes) / max(
            ctx.pm_depth_yes + ctx.pm_depth_no, 1e-6
        )
        # Positive weight → up pressure on YES.
        prob = max(0.0, min(1.0, mid + 0.1 * weight))
        conf = min(1.0, abs(weight))
        return prob, conf

    @staticmethod
    def _model_c_momentum(ctx: TickContext, spots: list[float]) -> tuple[float, float]:
        m90 = micro_feat.momentum_bps(spots, 90)
        m30 = micro_feat.momentum_bps(spots, 30)
        # Sign agreement + magnitude.
        agree = 1.0 if (m30 >= 0) == (m90 >= 0) else 0.0
        prob = 0.5 + 0.005 * m90  # 100 bps → 1.0 (saturated via clip)
        prob = max(0.0, min(1.0, prob))
        conf = min(1.0, abs(m90) / 40.0) * agree
        return prob, conf

    # ------------------------------- decision -------------------------------

    def should_enter(self, ctx: TickContext) -> Decision:
        cp_t = _nearest_checkpoint(ctx.t_in_window)
        if cp_t is None:
            return Decision(Action.SKIP, reason="outside_checkpoint")
        # One entry per market per strategy.
        if self._last_entered_per_market.get(ctx.market_slug):
            return Decision(Action.SKIP, reason="already_entered")

        spots = [
            t.spot_price for t in ctx.recent_ticks
            if hasattr(t, "ts") and (ctx.ts - t.ts) <= 90.0 and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < 30:
            return Decision(Action.SKIP, reason="insufficient_micro_data")

        macro_snap = self.macro.snapshot_at(ctx.ts) if self.macro else None
        regime_state = None
        if self.hmm is not None and macro_snap is not None:
            # Build a closes series from the macro provider by replaying
            # the cached candles via macro_snap proxies — we don't
            # actually have closes in MacroSnapshot. Fall back to spots
            # down-sampled to 5m (≈ span last 100 min of 1-Hz spots).
            if len(spots) >= 60:
                sampled = spots[::30]  # rough 30-second down-sample
                regime_state = self.hmm.predict(sampled)

        if regime_state is not None and regime_state.label == "high_vol":
            self._last_entered_per_market[ctx.market_slug] = True
            return Decision(Action.SKIP, reason="high_vol_abstain")

        pa_prob, pa_conf = self._model_a_ofi(ctx)
        pb_prob, pb_conf = self._model_b_microprice(ctx)
        pc_prob, pc_conf = self._model_c_momentum(ctx, spots)

        # Fallback ensemble when meta model not loaded.
        if self.model is None:
            weights = (pa_conf, pb_conf, pc_conf)
            total_w = sum(weights) or 1.0
            p_raw = (pa_prob * pa_conf + pb_prob * pb_conf + pc_prob * pc_conf) / total_w
        else:
            feature_vec = self._build_meta_features(
                ctx, cp_t, regime_state, macro_snap,
                [(pa_prob, pa_conf), (pb_prob, pb_conf), (pc_prob, pc_conf)],
            )
            p_raw = self.model.predict_proba(feature_vec)

        decision_label, p_cal = self.conformal.decide(p_raw)
        features = {
            "cp_t": cp_t,
            "p_raw": p_raw,
            "p_cal": p_cal,
            "pa_prob": pa_prob, "pa_conf": pa_conf,
            "pb_prob": pb_prob, "pb_conf": pb_conf,
            "pc_prob": pc_prob, "pc_conf": pc_conf,
            "regime": regime_state.label if regime_state else "unknown",
        }

        if decision_label == "abstain":
            if ctx.t_in_window >= CHECKPOINTS[-1] + CHECKPOINT_TOL_S:
                self._last_entered_per_market[ctx.market_slug] = True
                return Decision(
                    Action.SKIP, reason="contest_abstained",
                    signal_features=features,
                )
            return Decision(
                Action.SKIP, reason="conformal_abstain",
                signal_features=features,
            )

        self._last_entered_per_market[ctx.market_slug] = True
        side = Side.YES_UP if decision_label == "predict_up" else Side.YES_DOWN
        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            signal_breakdown={"cp_t": cp_t, "p_cal": p_cal},
            reason=f"{decision_label} cp={cp_t:.0f} p_cal={p_cal:.3f}",
        )

    # -------- meta-feature builder ---------------------------------------

    @staticmethod
    def _build_meta_features(
        ctx, cp_t, regime_state, macro_snap, l3_outs,
    ) -> list[float]:
        regime_onehot = [0.0, 0.0, 0.0, 0.0]  # bull, bear, ranging, high_vol
        if regime_state is not None:
            regime_onehot = list(regime_state.posteriors)
        pa, pb, pc = l3_outs
        ema_pct = macro_snap.ema8_vs_ema34_pct if macro_snap else 0.0
        adx = macro_snap.adx_14 if macro_snap else 0.0
        return [
            cp_t / 300.0,
            pa[0], pa[1], pb[0], pb[1], pc[0], pc[1],
            *regime_onehot,
            ema_pct, adx,
            float(ctx.pm_spread_bps),
            float(ctx.implied_prob_yes),
        ]


def load_meta_model_async_factory():
    """Returns a coroutine that looks up the active meta model row.

    Kept as a factory to mirror ``last_90s_forecaster_v2.load_runner_async``.
    """

    async def _loader():
        from trading.common.db import acquire
        from trading.common.logging import get_logger

        log = get_logger("strategy.contest_ensemble_v1")
        try:
            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT path FROM research.models "
                    "WHERE name = $1 AND is_active = TRUE",
                    "contest_ensemble_v1_meta",
                )
        except Exception as e:
            log.warning("meta.lookup_err", err=str(e))
            return None
        if row is None:
            return None
        model_file = Path(row["path"]) / "model.lgb"
        if not model_file.exists():
            log.warning("meta.model_missing", path=str(model_file))
            return None
        import lightgbm as lgb
        booster = lgb.Booster(model_file=str(model_file))

        class _Runner:
            def predict_proba(self, x):
                import numpy as np
                arr = np.asarray([x], dtype=np.float64)
                return float(booster.predict(arr)[0])

        return _Runner()

    return _loader
