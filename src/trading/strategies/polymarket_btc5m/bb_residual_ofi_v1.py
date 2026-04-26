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

from pathlib import Path
from typing import Protocol

from trading.common.logging import get_logger
from trading.engine.features.binance_microstructure import (
    binance_microstructure_from_trades,
)
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.strategies.polymarket_btc5m._bb_ofi_features import (
    FEATURE_NAMES,
    BBOFIFeatureInputs,
    build_vector,
    convex_fee,
)
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
    LGBRunner,  # reused — same n_features guard as v2/v3
)

log = get_logger("strategy.bb_residual_ofi_v1")


class ModelRunner(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


class MicrostructureProviderLike(Protocol):
    def fetch(self, ts: float) -> dict[str, float]: ...


# Re-exported for tests + tooling that want the canonical helpers
# without going through the shared feature module by name.
_convex_fee = convex_fee


def _alpha_shrinkage(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Backwards-compatible wrapper kept so the existing unit tests
    that import the strategy-private helper still work without
    rewriting their imports."""
    from trading.strategies.polymarket_btc5m._bb_ofi_features import (
        alpha_shrinkage as _f,
    )

    return _f(*args, **kwargs)


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

    def _emit(self, ctx: TickContext, decision: Decision) -> Decision:
        """Single structured log emission per *informative* decision —
        i.e. one where the strategy computed at least the BB prior +
        microstructure block. Early-gate skips ("outside_entry_window",
        "spread_too_wide", "insufficient_micro_data") are not emitted
        because they're 90 % of ticks and would drown the signal.

        Tail with: ``scripts/watch_bb_ofi.py`` (or
        ``docker logs -f tea-engine 2>&1 | grep bb_ofi.decision``).
        """
        f = decision.signal_features or {}
        # ``side`` from the Decision is "NONE" on every SKIP path —
        # use the strategy-inferred better side from features when
        # available so the dashboard can colour the arrow even before
        # a real model is loaded.
        inferred_side = str(f.get("side_picked", decision.side.value))
        log.info(
            "bb_ofi.decision",
            slug=ctx.market_slug,
            t_in_window=round(float(ctx.t_in_window), 2),
            action=decision.action.value,
            side=inferred_side,
            reason=decision.reason,
            spot=round(float(ctx.spot_price), 2),
            open=round(float(ctx.open_price), 2),
            p_market=round(float(ctx.implied_prob_yes), 4),
            p_bm=round(float(f.get("bb_p_prior", 0.0)), 4),
            p_edge=round(float(f.get("p_edge", 0.0)), 4),
            p_final=round(float(f.get("p_final", 0.0)), 4),
            edge_net=round(float(f.get("edge_net", 0.0)), 4),
            sharpe=round(float(f.get("sharpe", 0.0)), 2),
            sharpe_th=round(float(f.get("sharpe_threshold_eff", 0.0)), 2),
            ofi=round(float(f.get("ofi_composite", 0.0)), 3),
            cvd_bm=round(float(f.get("ofi_composite", 0.0)), 3),
            taker_buy=round(float(f.get("bm_taker_buy_ratio", 0.5)), 3),
            intensity=round(float(f.get("bm_trade_intensity", 1.0)), 2),
            large_trade=int(f.get("bm_large_trade_flag", 0.0)),
            alpha=round(float(f.get("alpha_shrinkage", 0.0)), 2),
            fee=round(float(f.get("fee_at_market", 0.0)), 4),
            spread_bps=round(float(ctx.pm_spread_bps), 1),
            shadow=bool(f.get("shadow", False)),
        )
        return decision

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

        # Single source of truth — same builder that the training CLI
        # uses, so train/serve cannot drift.
        inputs = BBOFIFeatureInputs(
            spot_price=ctx.spot_price,
            open_price=ctx.open_price,
            t_in_window_s=ctx.t_in_window,
            spots_last_90s=spots,
            implied_prob_yes=ctx.implied_prob_yes,
            pm_spread_bps=ctx.pm_spread_bps,
            pm_imbalance=ctx.pm_imbalance,
            ms_features=ms,
            bb_T_seconds=bb_T,
            fee_k=fee_k,
            alpha_min=alpha_min,
            alpha_max=alpha_max,
            alpha_ofi_gain=alpha_ofi_gain,
            alpha_large_trade_bonus=alpha_large_trade_bonus,
            entry_window_start_s=entry_start,
            entry_window_end_s=entry_end,
        )
        # Need σ-validity before calling the model: realized_vol_per_sqrt_s
        # returns 0.0 when fewer than 30 valid log-returns exist, which
        # would make the BB prior degenerate to 0.5.
        if len(spots) >= 31:
            n_valid = sum(
                1 for i in range(1, len(spots)) if spots[i - 1] > 0 and spots[i] > 0
            )
        else:
            n_valid = 0
        if n_valid < 30:
            return Decision(Action.SKIP, reason="insufficient_returns")

        vec, features = build_vector(inputs)
        features["shadow"] = shadow
        # Local aliases for the legacy inline references below.
        p_bm = features["bb_p_prior"]
        ofi_composite = features["ofi_composite"]
        alpha = features["alpha_shrinkage"]
        vol_per_sqrt_s = features["vol_per_sqrt_s"]
        p_market = float(ctx.implied_prob_yes)
        fee = features["fee_at_market"]

        if self.model is None:
            # Honest no-edge identity: p_edge ≡ p_bm so we never silently
            # invent edge from the prior alone. We still pick the
            # better-side edge so the shadow log + dashboard show a
            # meaningful direction (the BB prior alone tells you
            # whether spot has drifted above or below the strike).
            features["p_edge"] = p_bm
            features["p_final"] = p_bm
            edge_yes_nm = (p_bm - p_market) - fee
            edge_no_nm = (p_market - p_bm) - fee
            if edge_yes_nm >= edge_no_nm:
                features["edge_net"] = edge_yes_nm
                features["side_picked"] = Side.YES_UP.value
            else:
                features["edge_net"] = edge_no_nm
                features["side_picked"] = Side.YES_DOWN.value
            return self._emit(
                ctx,
                Decision(
                    Action.SKIP,
                    reason="shadow_mode_no_model",
                    signal_features=features,
                ),
            )

        try:
            p_edge = float(self.model.predict_proba(vec))
        except Exception as e:
            log.warning("bb_ofi.model_predict_err", err=str(e))
            return self._emit(
                ctx,
                Decision(
                    Action.SKIP,
                    reason="model_predict_err",
                    signal_features=features,
                ),
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
            return self._emit(
                ctx,
                Decision(
                    Action.SKIP,
                    reason="edge_net_below_floor",
                    signal_features=features,
                ),
            )
        if sharpe < sharpe_th_eff:
            return self._emit(
                ctx,
                Decision(
                    Action.SKIP,
                    reason="sharpe_below_threshold",
                    signal_features=features,
                ),
            )

        if shadow:
            return self._emit(
                ctx,
                Decision(
                    Action.SKIP,
                    reason="shadow_mode",
                    signal_features=features,
                ),
            )

        self._per_window_entered.add(ctx.market_slug)
        return self._emit(
            ctx,
            Decision(
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
