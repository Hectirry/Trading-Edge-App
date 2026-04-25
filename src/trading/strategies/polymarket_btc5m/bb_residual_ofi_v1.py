"""bb_residual_ofi_v1 — Brownian-bridge baseline + OFI residual.

Hypothesis: at any t in [60, 290] s of a 5 m BTC up/down window, the
Polymarket implied probability lags Binance microstructure
(taker-tape CVD = OFI proxy + trade intensity + large-trade flag).
A no-drift Brownian-bridge prior on Binance spot, shrinkage-blended
with a calibrated ensemble of microstructure features, gives an
edge net of the convex fee that — gated by Sharpe-per-trade ≥ θ —
clears the round-trip cost only ~25 % of windows but compounds to a
Sharpe ≫ 1 over hundreds of trades.

This file is the *serving scaffold*. Same shadow-degrade pattern as
last_90s_forecaster_v3:

- No trained ensemble exists yet → ``self.model is None`` →
  ``p_edge ≡ p_bm`` (no edge, deliberate identity) → SKIP
  ``shadow_mode_no_model``.
- Feature vector + edge_net + sharpe are computed every tick and
  attached to the Decision via ``signal_features``. The PaperDriver
  today only aggregates SKIP reasons as Prometheus counters — it
  does NOT persist per-tick feature dicts. Training data comes from
  offline reconstruction (``paper_ticks`` + ``crypto_trades`` +
  ``market_outcomes``), same path v3 uses. A per-decision writer is
  a separate ADR (sized vs. write-amp on a 1 Hz tick stream).
- Even when a model promotes, ``[paper].shadow=true`` keeps the
  strategy off the trade path until walk-forward validation passes
  (``.claude/skills/tea-promotion-gate``).

Honesty caveats baked into the code (not buried in a comment far away):

- The user spec describes an OFI "compuesto" β1·OFI_binance + β2·OFI_coinbase.
  We have ``market_data.crypto_trades`` for Binance only. Coinbase
  trades ingestion is a separate ADR. For now ``ofi_composite`` ≡
  Binance CVD over the last ``microstructure_window_seconds``. The
  TOML param ``ofi_coinbase_weight`` is plumbed but treated as a
  sentinel (must be 0.0 until ingestion lands).
- ``p_edge_sigma`` (denominator of the per-trade Sharpe) is a fixed
  TOML sentinel (default 0.025) until the bootstrap ensemble is
  trained and emits a real per-prediction stddev. Loud comment in the
  TOML so this is not silently load-bearing.
- The shrinkage coefficient ``α`` is a deterministic function of
  |OFI|, large-trade flag, and t_in_window — not learned. Once the
  ensemble exists and emits its own variance, α should be derived
  from ensemble dispersion (smaller stddev → higher α). Keeping it
  rules-based for v1 means we don't pretend to have information we
  haven't trained yet.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from trading.common.logging import get_logger
from trading.engine.features.bb_residual import brownian_bridge_prob
from trading.engine.features.binance_microstructure import (
    binance_microstructure_from_trades,
)
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
    LGBRunner,  # reused — same n_features guard as v2/v3
)

log = get_logger("strategy.bb_residual_ofi_v1")


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


class ModelRunner(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


class MicrostructureProviderLike(Protocol):
    def fetch(self, ts: float) -> dict[str, float]: ...


def _convex_fee(p_market: float, fee_k: float) -> float:
    """Spec fee model: ``fee(p) = fee_k · 4·p·(1-p)`` — convex, peaks
    at p=0.5 (=fee_k), zero at the corners. ``fee_k=0.0315`` matches
    the worked example in estrategias/en-desarrollo/bb_residual_ofi_v1.md."""
    p = max(0.0, min(1.0, p_market))
    return fee_k * 4.0 * p * (1.0 - p)


def _alpha_shrinkage(
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
    accumulates. Linear in |OFI|, +bonus on a large-trade event, and
    a small linear ramp with t_in_window so early-window ticks lean
    on the BB prior. Clamped to [alpha_min, alpha_max].

    Replace with ensemble-variance-driven α once the ensemble is
    trained — see strategy docstring for why this is rules-based today.
    """
    span = max(1.0, entry_end_s - entry_start_s)
    t_norm = max(0.0, min(1.0, (t_in_window_s - entry_start_s) / span))
    base = alpha_min + (alpha_max - alpha_min) * 0.5 * t_norm
    bonus = ofi_gain * min(1.0, ofi_abs) + large_trade_bonus * large_trade_flag
    return float(max(alpha_min, min(alpha_max, base + bonus)))


class BBResidualOFIV1(StrategyBase):
    name = "bb_residual_ofi_v1"

    def __init__(
        self,
        config: dict,
        model: ModelRunner | None = None,
        microstructure_provider: MicrostructureProviderLike | None = None,
    ) -> None:
        super().__init__(config)
        self.model = model
        self.ms_provider = microstructure_provider
        self._per_window_entered: set[str] = set()

    def notify_window_rollover(self, new_slug: str) -> None:
        self._per_window_entered.clear()

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params
        entry_start = float(p.get("entry_window_start_s", 60))
        entry_end = float(p.get("entry_window_end_s", 290))
        sharpe_th = float(p.get("sharpe_threshold", 2.0))
        sharpe_th_late = float(p.get("sharpe_threshold_late", 1.5))
        late_window_s = float(p.get("sharpe_late_t_to_close_s", 30.0))
        edge_net_min = float(p.get("edge_net_min", 0.0))
        spread_max = float(p.get("spread_max_bps", 300.0))
        bb_T = float(p.get("bb_T_seconds", 300.0))
        fee_k = float(p.get("fee_k", 0.0315))
        ms_window_s = int(p.get("microstructure_window_seconds", 30))
        large_threshold_usd = float(p.get("large_trade_threshold_usd", 100_000.0))
        ofi_coinbase_weight = float(p.get("ofi_coinbase_weight", 0.0))
        p_edge_sigma = float(p.get("p_edge_sigma", 0.025))
        alpha_min = float(p.get("alpha_min", 0.4))
        alpha_max = float(p.get("alpha_max", 0.85))
        alpha_ofi_gain = float(p.get("alpha_ofi_gain", 1.0))
        alpha_large_trade_bonus = float(p.get("alpha_large_trade_bonus", 0.1))
        shadow = bool(self.config.get("paper", {}).get("shadow", True))

        if not (entry_start <= ctx.t_in_window <= entry_end):
            return Decision(Action.SKIP, reason="outside_entry_window")

        if ctx.market_slug in self._per_window_entered:
            return Decision(Action.SKIP, reason="already_entered_this_window")

        if ctx.pm_spread_bps > spread_max:
            return Decision(Action.SKIP, reason="spread_too_wide")

        # Sentinel until Coinbase trade ingestion lands. A non-zero value
        # would silently mix unsourced data into the OFI signal.
        if ofi_coinbase_weight != 0.0:
            return Decision(
                Action.SKIP,
                reason="ofi_coinbase_weight_must_be_zero_until_ingest_lands",
            )

        spots = [
            t.spot_price
            for t in ctx.recent_ticks
            if hasattr(t, "ts") and (ctx.ts - t.ts) <= 90.0 and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < 60:
            return Decision(Action.SKIP, reason="insufficient_micro_data")

        # σ per sqrt(second) from 1 Hz log-returns over the last 90 s,
        # same scale brownian_bridge_prob expects.
        rets = [
            math.log(spots[i] / spots[i - 1])
            for i in range(1, len(spots))
            if spots[i - 1] > 0 and spots[i] > 0
        ]
        if len(rets) < 30:
            return Decision(Action.SKIP, reason="insufficient_returns")
        mu_r = sum(rets) / len(rets)
        var_r = sum((r - mu_r) ** 2 for r in rets) / len(rets)
        vol_per_sqrt_s = math.sqrt(max(var_r, 0.0))

        p_bm = brownian_bridge_prob(
            spot=ctx.spot_price,
            open_=ctx.open_price,
            t_in_window_s=ctx.t_in_window,
            vol_per_sqrt_s=vol_per_sqrt_s,
            T=bb_T,
        )
        # Normalised window delta (z-score of the no-drift bridge). Logged
        # for the paper-prediction trail; not consumed by p_final.
        denom = (
            ctx.open_price * vol_per_sqrt_s * math.sqrt(max(bb_T - ctx.t_in_window, 1e-6))
            if ctx.open_price > 0 and vol_per_sqrt_s > 0
            else 0.0
        )
        delta_norm = (ctx.spot_price - ctx.open_price) / denom if denom > 0 else 0.0

        if self.ms_provider is not None:
            ms = self.ms_provider.fetch(ctx.ts)
        else:
            # Shadow boot path: no sync MS provider wired yet. Sentinels
            # ensure the vector is well-formed; SKIP("shadow_mode_no_model")
            # below means the model is never asked.
            ms = binance_microstructure_from_trades(
                trades=[],
                baseline_trades_24h=0,
                window_s=ms_window_s,
                large_threshold_usd=large_threshold_usd,
            )

        ofi_binance = float(ms["bm_cvd_normalized"])
        ofi_composite = ofi_binance  # ofi_coinbase_weight enforced 0 above
        alpha = _alpha_shrinkage(
            ofi_abs=abs(ofi_composite),
            large_trade_flag=float(ms["bm_large_trade_flag"]),
            t_in_window_s=ctx.t_in_window,
            entry_start_s=entry_start,
            entry_end_s=entry_end,
            alpha_min=alpha_min,
            alpha_max=alpha_max,
            ofi_gain=alpha_ofi_gain,
            large_trade_bonus=alpha_large_trade_bonus,
        )

        p_market = float(ctx.implied_prob_yes)
        fee = _convex_fee(p_market, fee_k)

        vec = [
            p_bm,
            delta_norm,
            ofi_composite,
            float(ms["bm_taker_buy_ratio"]),
            float(ms["bm_trade_intensity"]),
            float(ms["bm_large_trade_flag"]),
            float(ms["bm_signed_autocorr_lag1"]),
            p_market,
            float(ctx.pm_spread_bps),
            float(ctx.pm_imbalance),
            float(ctx.t_in_window),
            vol_per_sqrt_s,
            fee,
            alpha,
        ]
        features = dict(zip(FEATURE_NAMES, vec, strict=True))
        features["shadow"] = shadow

        if self.model is None:
            # Honest no-edge identity: p_edge ≡ p_bm so we never silently
            # invent edge from the prior alone.
            features["p_edge"] = p_bm
            features["p_final"] = p_bm
            features["edge_net"] = (p_bm - p_market) - fee
            return Decision(
                Action.SKIP,
                reason="shadow_mode_no_model",
                signal_features=features,
            )

        try:
            p_edge = float(self.model.predict_proba(vec))
        except Exception as e:
            log.warning("bb_ofi.model_predict_err", err=str(e))
            return Decision(
                Action.SKIP,
                reason="model_predict_err",
                signal_features=features,
            )
        p_edge = max(0.0, min(1.0, p_edge))
        p_final = alpha * p_edge + (1.0 - alpha) * p_bm

        # Edge can be on either side. Pick the side the model favours
        # (compared to the market net of the symmetric convex fee), and
        # drop trades whose net edge does not clear the configured floor.
        edge_yes = (p_final - p_market) - fee
        edge_no = ((1.0 - p_final) - (1.0 - p_market)) - fee  # = -(p_final - p_market) - fee
        if edge_yes >= edge_no:
            side = Side.YES_UP
            edge_net = edge_yes
        else:
            side = Side.YES_DOWN
            edge_net = edge_no

        sharpe = edge_net / p_edge_sigma if p_edge_sigma > 0 else 0.0
        # Adaptive Sharpe gate: relax to sharpe_th_late when the window
        # is about to close. The strict θ is what backtests are trained
        # for; the relaxed θ is the "now-or-never" branch the spec calls
        # out — hand-raised to avoid the trivial edge-cliff at t=T-eps.
        t_to_close = max(0.0, bb_T - ctx.t_in_window)
        sharpe_th_eff = sharpe_th_late if t_to_close <= late_window_s else sharpe_th

        features.update(
            {
                "p_edge": p_edge,
                "p_final": p_final,
                "edge_net": edge_net,
                "sharpe": sharpe,
                "sharpe_threshold_eff": sharpe_th_eff,
                "side_picked": side.value,
            }
        )

        if edge_net < edge_net_min:
            return Decision(
                Action.SKIP,
                reason="edge_net_below_floor",
                signal_features=features,
            )
        if sharpe < sharpe_th_eff:
            return Decision(
                Action.SKIP,
                reason="sharpe_below_threshold",
                signal_features=features,
            )

        if shadow:
            return Decision(
                Action.SKIP,
                reason="shadow_mode",
                signal_features=features,
            )

        self._per_window_entered.add(ctx.market_slug)
        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            signal_breakdown={
                "edge_net": edge_net,
                "sharpe": sharpe,
                "p_final": p_final,
                "p_bm": p_bm,
                "alpha": alpha,
            },
            reason=(
                f"edge_net={edge_net:+.4f} sharpe={sharpe:.2f} "
                f"p_final={p_final:.4f} p_bm={p_bm:.4f} alpha={alpha:.2f}"
            ),
        )


async def load_runner_async(
    name: str = "bb_residual_ofi_v1",
) -> ModelRunner | None:
    """Async lookup mirroring v2/v3. Reuses ``LGBRunner`` so the
    ``num_feature()`` guard is identical across families. Returns
    ``None`` when no ``is_active=true`` row exists — strategy boots
    shadow."""
    from trading.common.db import acquire

    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT path FROM research.models WHERE name = $1 AND is_active = TRUE",
                name,
            )
    except Exception as e:
        log.warning("bb_ofi.model_lookup_err", err=str(e))
        return None
    if row is None:
        log.info("bb_ofi.no_active_model_row", name=name)
        return None
    path = Path(row["path"])
    model_file = path / "model.lgb"
    calibrator_file = path / "calibrator.pkl"
    if not model_file.exists():
        log.error("bb_ofi.model_file_missing", path=str(model_file))
        return None
    try:
        return LGBRunner(model_file, calibrator_path=calibrator_file)
    except Exception as e:
        log.error("bb_ofi.model_load_err", err=str(e), path=str(model_file))
        return None


def load_runner_from_registry(
    name: str = "bb_residual_ofi_v1",
) -> ModelRunner | None:
    import asyncio

    return asyncio.run(load_runner_async(name))
