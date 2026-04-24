"""contest_avengers_v1 — asymmetric information (ADR 0012).

Selective PREDICT via a hard confidence threshold built from
- Chainlink lag (oracle catching up to Binance spot) — core signal
- Liquidation-cluster gravity (Coinalyze, via cached provider)
- HMM regime adjustment (skip on high_vol, nudge otherwise)
- OFI tie-breaker (only when Chainlink synced)

Checkpoints: ``t_in_window ∈ {180, 210, 240, 270}``.
"""

from __future__ import annotations

from typing import Protocol

from trading.engine.features import hmm_regime as hmm
from trading.engine.features.chainlink_oracle import (
    binance_chainlink_delta_bps,
    chainlink_lag_score,
)
from trading.engine.features.liquidation_gravity import (
    gravity_scores,
    signed_gravity,
)
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext

CHECKPOINTS = (180.0, 210.0, 240.0, 270.0)
CHECKPOINT_TOL_S = 3.0


class MacroProviderLike(Protocol):
    def snapshot_at(self, as_of_ts: float): ...


class ChainlinkSnapshotProviderLike(Protocol):
    def snapshot(self) -> dict | None: ...


class LiqClustersProviderLike(Protocol):
    def snapshot(self) -> list: ...


def _nearest_checkpoint(t_in_window: float) -> float | None:
    for cp_t in CHECKPOINTS:
        if abs(t_in_window - cp_t) <= CHECKPOINT_TOL_S:
            return cp_t
    return None


class ContestAvengersV1(StrategyBase):
    name = "contest_avengers_v1"

    def __init__(
        self,
        config: dict,
        *,
        macro_provider: MacroProviderLike | None = None,
        hmm_detector: object | None = None,
        chainlink_provider: ChainlinkSnapshotProviderLike | None = None,
        liq_provider: LiqClustersProviderLike | None = None,
    ) -> None:
        super().__init__(config)
        self.macro = macro_provider
        self.hmm = hmm_detector or hmm.NullHMMRegimeDetector()
        self.chainlink = chainlink_provider
        self.liq = liq_provider
        self._last_entered_per_market: dict[str, bool] = {}

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params
        confidence_threshold = float(p.get("confidence_threshold", 0.75))

        cp_t = _nearest_checkpoint(ctx.t_in_window)
        if cp_t is None:
            return Decision(Action.SKIP, reason="outside_checkpoint")
        if self._last_entered_per_market.get(ctx.market_slug):
            return Decision(Action.SKIP, reason="already_entered")

        # --- Chainlink lag signal ---
        chainlink_score = 0.0
        chainlink_sign = 0
        cl = self.chainlink.snapshot() if self.chainlink else None
        if cl is not None:
            delta_bps = binance_chainlink_delta_bps(ctx.spot_price, cl["answer"])
            chainlink_score = chainlink_lag_score(cl["age_s"], delta_bps)
            chainlink_sign = 1 if delta_bps > 0 else (-1 if delta_bps < 0 else 0)

        # --- Liquidation gravity ---
        liq_score = 0.0
        liq_sign = 0
        clusters = self.liq.snapshot() if self.liq else []
        if clusters:
            down, up = gravity_scores(ctx.spot_price, clusters)
            liq_score = abs(signed_gravity(down, up))
            liq_sign = 1 if up > down else (-1 if down > up else 0)

        # --- HMM ---
        hmm_adjustment = 0.0
        regime_label = "unknown"
        if self.hmm is not None:
            # Build a coarse closes series from recent_ticks spots down-sampled to ~5s.
            spots = [
                t.spot_price for t in ctx.recent_ticks if hasattr(t, "ts") and t.spot_price > 0
            ]
            spots.append(ctx.spot_price)
            sampled = spots[::5] if len(spots) >= 40 else []
            if sampled:
                state = self.hmm.predict(sampled)
                if state is not None:
                    regime_label = state.label
                    if state.label == "high_vol":
                        self._last_entered_per_market[ctx.market_slug] = True
                        return Decision(
                            Action.SKIP,
                            reason="high_vol_skip",
                            signal_features={"regime": regime_label},
                        )
                    if state.label == "ranging":
                        hmm_adjustment = -0.15
                    elif state.label in ("trending_bull", "trending_bear"):
                        # Align bonus only if the trend matches chainlink sign.
                        want = 1 if state.label == "trending_bull" else -1
                        if chainlink_sign == want:
                            hmm_adjustment = 0.10

        # --- OFI tie-breaker (only when Chainlink synced) ---
        ofi_score = 0.0
        ofi_sign = 0
        if cl is not None and cl["age_s"] < 3:
            imb = float(ctx.pm_imbalance or 0.0)
            ofi_score = min(1.0, abs(imb))
            ofi_sign = 1 if imb > 0 else (-1 if imb < 0 else 0)

        # --- Aggregate confidence ---
        # Same-sign check for liquidation component.
        liq_component = liq_score if liq_sign == chainlink_sign and chainlink_sign != 0 else 0.0
        confidence_mag = (
            0.50 * chainlink_score + 0.25 * liq_component + 0.15 * hmm_adjustment + 0.10 * ofi_score
        )
        # Graceful degradation cap (ADR 0012): if Coinalyze OR Chainlink
        # missing, cap confidence at 0.85 so we never emit a full-
        # confidence PREDICT on partial evidence.
        degraded = (cl is None) or (not clusters)
        if degraded:
            confidence_mag = min(confidence_mag, 0.85)

        # Direction from Chainlink lag (primary); fallback to OFI sign if no CL.
        direction = chainlink_sign or ofi_sign

        features = {
            "cp_t": cp_t,
            "chainlink_score": chainlink_score,
            "chainlink_sign": chainlink_sign,
            "liq_score": liq_score,
            "liq_sign": liq_sign,
            "ofi_score": ofi_score,
            "ofi_sign": ofi_sign,
            "hmm_adj": hmm_adjustment,
            "regime": regime_label,
            "confidence_mag": confidence_mag,
            "direction": direction,
            "degraded": degraded,
        }

        if direction == 0:
            if ctx.t_in_window >= CHECKPOINTS[-1] + CHECKPOINT_TOL_S:
                self._last_entered_per_market[ctx.market_slug] = True
                return Decision(
                    Action.SKIP,
                    reason="no_direction_at_last_checkpoint",
                    signal_features=features,
                )
            return Decision(
                Action.SKIP,
                reason="awaiting_directional_signal",
                signal_features=features,
            )

        if confidence_mag < confidence_threshold:
            if ctx.t_in_window >= CHECKPOINTS[-1] + CHECKPOINT_TOL_S:
                self._last_entered_per_market[ctx.market_slug] = True
                return Decision(
                    Action.SKIP,
                    reason="confidence_below_threshold_final",
                    signal_features=features,
                )
            return Decision(
                Action.SKIP,
                reason="confidence_below_threshold",
                signal_features=features,
            )

        self._last_entered_per_market[ctx.market_slug] = True
        side = Side.YES_UP if direction > 0 else Side.YES_DOWN
        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            signal_breakdown={
                "cp_t": cp_t,
                "confidence_mag": confidence_mag,
                "direction": direction,
            },
            reason=(
                f"confidence={confidence_mag:.3f} sign={direction:+d}" f" regime={regime_label}"
            ),
        )
