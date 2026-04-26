"""oracle_lag_v1 — analytic Black-Scholes binary digital on the
Chainlink-vs-spot residual.

Hypothesis (ADR 0013, brief 2026-04-26): the winning bots on Polymarket
BTC up/down 5 m do not predict BTC; they predict the next Chainlink
Data Streams report. That report is the median across ~16 DON node
operators of liquidity-weighted bid/ask snapshots from a multi-CEX
USD basket — and the median lags the underlying exchange order books
by hundreds of milliseconds to ~1 s. By computing our own multi-CEX
fair price ``P_spot`` and comparing it to the window-open oracle
reference, we estimate P(close > strike) before Polymarket's book
prices it in.

Phase 0 (this file, Sprint 1): single-venue Binance spot only. The
USDT-vs-USD correction and Coinbase/OKX/Kraken venues are deferred to
Sprint 2-5. If the analytic form does not show edge here on its own,
adding venues will not rescue it (kill-switch in ADR 0013).

Decision flow:
  1. ``t_in_window`` ∈ [entry_window_start_s, entry_window_end_s]
     (default [285, 297] = T-15s to T-3s).
  2. Rebuild ``δ = (P_spot − P_open) / P_open``. P_open from
     ``ctx.open_price`` (already canonical settle source). P_spot from
     ``ctx.spot_price`` (live Binance kline_1s mid). USDT basis fixed
     at 1.0 in Phase 0.
  3. σ_per_sqrt_s from ``sigma_ewma`` over the last 90 s of
     ``ctx.recent_ticks``. Need ≥ 60 ticks; else SKIP.
  4. ``p_up = Φ(δ / (σ √τ))`` via ``black_scholes_digital.p_up``.
  5. Side = YES_UP if p_up > 0.5 else YES_DOWN. Pick the matching
     ``ask`` from the Polymarket book.
  6. ``EV = p_side · (1 − ask) − (1 − p_side) · ask``.
  7. Subtract dynamic taker fee ``fee(p) = fee_a + fee_b · 4·p·(1−p)``.
     With fee_a=0.005, fee_b=0.025 the peak is 3.0 % at p=0.5,
     reproducing the ~1.5-3.0 % range of the 2026 dynamic-fee regime.
  8. Gate ``EV_neto ≥ ev_threshold`` (default 0.005 USD/share, half a
     cent). Otherwise SKIP.

The strategy emits SKIP under ``[paper] shadow = true`` (default in
v1) — features are still attached to the Decision so the engine logs
them for offline calibration. Promotion to ``shadow = false`` is the
ADR-0011-style gate that requires explicit operator action.
"""

from __future__ import annotations

from trading.common.logging import get_logger
from trading.engine.features.black_scholes_digital import p_up, sigma_ewma, tick_rule_cvd
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.strategies.polymarket_btc5m._oracle_lag_cesta import CestaProvider

log = get_logger("strategy.oracle_lag_v1")


def _dynamic_fee(p: float, fee_a: float, fee_b: float) -> float:
    """Polymarket 2026 taker fee model (parabola peaking at p=0.5).

    fee(p) = fee_a + fee_b · 4·p·(1−p)

    Defaults match the brief's reported range: floor 0.5 % at the
    extremes, peak ~3.0 % at p=0.5.
    """
    return fee_a + fee_b * 4.0 * p * (1.0 - p)


class OracleLagV1(StrategyBase):
    name = "oracle_lag_v1"

    def __init__(self, config: dict, cesta: CestaProvider | None = None) -> None:
        super().__init__(config)
        p = self.params
        self.entry_start_s = float(p.get("entry_window_start_s", 285.0))
        self.entry_end_s = float(p.get("entry_window_end_s", 297.0))
        self.sigma_lookback_s = float(p.get("sigma_lookback_s", 90.0))
        self.sigma_min_ticks = int(p.get("sigma_min_ticks", 60))
        self.ewma_lambda = float(p.get("ewma_lambda", 0.94))
        self.fee_a = float(p.get("fee_a", 0.005))
        self.fee_b = float(p.get("fee_b", 0.025))
        self.ev_threshold = float(p.get("ev_threshold", 0.005))
        # Sprint A.2 — entry-price ceiling. Backtest 2026-04-26 showed
        # 95.6 % of PnL came from trades with entry < $0.30; trades with
        # entry ≥ $0.70 contributed essentially zero net PnL while
        # adding noise. Default 1.0 keeps backwards-compat (no filter).
        self.max_entry_price = float(p.get("max_entry_price", 1.0))
        # Sprint B.1 — OFI confirmation gate. Tick-rule proxy of CVD
        # over the last ``ofi_window_s`` of spot ticks. If enabled,
        # SKIP whenever sign(δ) disagrees with sign(tick-rule CVD).
        self.ofi_enabled = bool(p.get("ofi_enabled", False))
        self.ofi_window_s = float(p.get("ofi_window_s", 30.0))
        self.ofi_min_strength = float(p.get("ofi_min_strength", 0.10))
        # Phase 0 fallback when no CestaProvider is injected.
        self.usdt_basis_phase0 = float(p.get("usdt_basis_phase0", 1.0))
        self.cesta = cesta
        self._per_window_entered: set[str] = set()

    def on_start(self) -> None:
        self._per_window_entered.clear()

    def should_enter(self, ctx: TickContext) -> Decision:
        # 1. Entry window
        if not (self.entry_start_s <= ctx.t_in_window <= self.entry_end_s):
            return Decision(Action.SKIP, reason="outside_entry_window")

        # 2. One entry per market
        if ctx.market_slug in self._per_window_entered:
            return Decision(Action.SKIP, reason="already_entered")

        # 3. Rebuild δ. Phase 1: cesta-weighted P_spot (Binance corregido
        # + Coinbase). Phase 0 fallback: Binance / hardcoded basis.
        if ctx.open_price <= 0 or ctx.spot_price <= 0:
            return Decision(Action.SKIP, reason="bad_price_data")
        cesta_dbg: dict | None = None
        if self.cesta is not None:
            p_spot_usd, cesta_dbg = self.cesta.p_spot(ctx.ts, ctx.spot_price)
        else:
            p_spot_usd = ctx.spot_price / self.usdt_basis_phase0
        p_open_usd = ctx.open_price  # canonical from settle source
        delta_pct = (p_spot_usd - p_open_usd) / p_open_usd

        # 4. σ EWMA from the last `sigma_lookback_s` of recent_ticks.
        cutoff_ts = ctx.ts - self.sigma_lookback_s
        spots = [t.spot_price for t in ctx.recent_ticks if t.ts >= cutoff_ts and t.spot_price > 0]
        spots.append(ctx.spot_price)
        if len(spots) < self.sigma_min_ticks:
            return Decision(Action.SKIP, reason="insufficient_sigma_ticks")
        sigma = sigma_ewma(spots, lam=self.ewma_lambda)
        if sigma <= 0:
            return Decision(Action.SKIP, reason="sigma_collapsed")

        # 5. τ = window_close - now (cap at 0).
        tau = max(0.0, ctx.window_close_ts - ctx.ts)
        prob_up = p_up(delta_pct=delta_pct, tau_s=tau, sigma_per_sqrt_s=sigma)

        # 5b. OFI confirmation gate (Sprint B.1). Tick-rule on the last
        # ofi_window_s of spot history; require directional agreement
        # with δ before allowing entry. ``ofi_min_strength`` is a
        # threshold on |CVD| — too-weak signal counts as "no opinion"
        # and falls through.
        cvd_proxy = 0.0
        if self.ofi_enabled and tau < 60.0:
            ofi_cutoff = ctx.ts - self.ofi_window_s
            ofi_spots = [
                t.spot_price
                for t in ctx.recent_ticks
                if t.ts >= ofi_cutoff and t.spot_price > 0
            ]
            ofi_spots.append(ctx.spot_price)
            cvd_proxy = tick_rule_cvd(ofi_spots)
            if abs(cvd_proxy) >= self.ofi_min_strength:
                if (delta_pct > 0) != (cvd_proxy > 0):
                    return Decision(
                        Action.SKIP,
                        reason="ofi_disagrees_with_delta",
                        signal_features={
                            "delta_pct": delta_pct,
                            "cvd_proxy": cvd_proxy,
                        },
                    )

        # 6. Side selection + EV.
        if prob_up >= 0.5:
            side = Side.YES_UP
            ask = ctx.pm_yes_ask
            p_side = prob_up
        else:
            side = Side.YES_DOWN
            ask = ctx.pm_no_ask
            p_side = 1.0 - prob_up

        if ask <= 0 or ask >= 1.0:
            return Decision(Action.SKIP, reason="bad_ask")

        if ask > self.max_entry_price:
            return Decision(Action.SKIP, reason="ask_above_max_entry_price")

        ev_gross = p_side * (1.0 - ask) - (1.0 - p_side) * ask
        fee = _dynamic_fee(ask, self.fee_a, self.fee_b)
        ev_net = ev_gross - fee

        # Always attach the feature trail so paper/live can log it.
        features = {
            "delta_pct": delta_pct,
            "sigma_per_sqrt_s": sigma,
            "tau_s": tau,
            "prob_up": prob_up,
            "p_side": p_side,
            "ask": ask,
            "ev_gross": ev_gross,
            "fee": fee,
            "ev_net": ev_net,
            "n_sigma_ticks": len(spots),
            "cvd_proxy": cvd_proxy,
        }
        if cesta_dbg is not None:
            features.update({f"cesta_{k}": v for k, v in cesta_dbg.items()})

        if ev_net < self.ev_threshold:
            return Decision(
                Action.SKIP,
                reason="ev_below_threshold",
                signal_features=features,
            )

        # 7. Shadow gate (ADR-0011-style — promotion is a separate
        # operator action via [paper].shadow flip).
        shadow = bool(self.config.get("paper", {}).get("shadow", True))
        if shadow:
            return Decision(Action.SKIP, reason="shadow_mode", signal_features=features)

        self._per_window_entered.add(ctx.market_slug)
        return Decision(
            action=Action.ENTER,
            side=side,
            signal_features=features,
            reason=f"ev_net={ev_net:+.4f} prob_up={prob_up:.3f}",
        )
