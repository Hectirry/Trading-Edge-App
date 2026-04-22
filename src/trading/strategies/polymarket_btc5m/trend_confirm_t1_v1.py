"""
trend_confirm_t1_v1 — port from
/home/coder/BTC-Tendencia-5m/strategies/trend_confirm_t1_v1.py.

Literal behavioral port per the Phase 3.5 plan. Ordering of gates, default
thresholds, breakdown keys, and feature names match the source so parity
tests against `polybot-agent.db:trades` hold bit-for-bit.

Hypothesis: at T-90 s of the 5-minute window, bet on sign(delta_bps) when
≥3 of 4 AFML filters (frac-diff, ar_L, CUSUM rate, microprice) confirm
the direction. Chainlink-adverse gate at 15 bps blocks against oracle
divergence.

Decision path (unchanged from source):
  1. t_in_window in [entry_horizon ± tolerance]
  2. |delta_bps| >= delta_bps_min (direction source)
  3. Chainlink divergence gate (hard skip if cl_delta_bps adverse > 15 bps)
  4. fav_ask < max_price (price cap, side-dependent)
  5. ≥ min_confirmations of {f1_fracdiff, f2_autocorr, f3_cusum,
     f4_microprice, f6_candle} match side; f5_mc only counts if mc_shadow=false
"""

from __future__ import annotations

from trading.engine.afml_features import compute_afml_features
from trading.engine.monte_carlo import mc_bootstrap_prob_up
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, OrderType, Side, TickContext


class TrendConfirmT1V1(StrategyBase):
    name = "trend_confirm_t1_v1"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        p = self.params
        self.entry_horizon_s = int(p.get("entry_horizon_s", 240))
        self.horizon_tolerance_s = float(p.get("horizon_tolerance_s", 2.0))
        self.delta_bps_min = float(p.get("delta_bps_min", 3.0))
        self.max_price = float(p.get("max_price", 0.80))
        self.min_confirmations = int(p.get("min_confirmations", 3))
        self.cusum_min_rate = float(p.get("cusum_min_rate", 0.005))
        self.recent_window_s = float(p.get("recent_window_s", 60.0))
        self.doji_body_ratio_min = float(p.get("doji_body_ratio_min", 0.3))
        self.cl_adverse_max_bps = float(p.get("cl_adverse_max_bps", 15.0))

        # AFML feature params
        self.frac_d = float(p.get("frac_d", 0.4))
        self.frac_size = int(p.get("frac_size", 60))
        self.entropy_lookback = int(p.get("entropy_lookback", 60))
        self.cusum_threshold = float(p.get("cusum_threshold", 0.0005))
        self.cusum_lookback = int(p.get("cusum_lookback", 120))
        self.ar_lags = tuple(p.get("ar_lags", (1, 5, 15, 30)))
        self.autocorr_lag = int(p.get("autocorr_lag", 1))

        # Monte Carlo bootstrap
        self.mc_enabled = bool(p.get("mc_enabled", True))
        self.mc_shadow = bool(p.get("mc_shadow", True))
        self.mc_n_sims = int(p.get("mc_n_sims", 1000))
        self.mc_threshold = float(p.get("mc_threshold", 0.55))

        # Order routing
        self.order_type_str = str(p.get("order_type", "gtc")).lower()
        self.limit_offset_bps = float(p.get("limit_offset_bps", 50.0))
        self.gtc_ttl_seconds = int(p.get("gtc_ttl_seconds", 30))

        self._per_window_entered: set[str] = set()

    def _is_horizon(self, ctx: TickContext) -> bool:
        return abs(ctx.t_in_window - self.entry_horizon_s) <= self.horizon_tolerance_s

    def should_enter(self, ctx: TickContext) -> Decision:
        if not self._is_horizon(ctx):
            return Decision(
                action=Action.SKIP,
                reason=(
                    f"not_at_horizon t={ctx.t_in_window:.1f}s (target={self.entry_horizon_s}s)"
                ),
            )
        if ctx.market_slug in self._per_window_entered:
            return Decision(action=Action.SKIP, reason="already_entered_this_window")

        # Gate 1: direction by delta_bps
        if ctx.delta_bps > self.delta_bps_min:
            side = Side.YES_UP
            want_positive = True
        elif ctx.delta_bps < -self.delta_bps_min:
            side = Side.YES_DOWN
            want_positive = False
        else:
            return Decision(
                action=Action.SKIP,
                reason=(f"indeciso delta_bps={ctx.delta_bps:+.1f} (|min|={self.delta_bps_min})"),
                signal_features={
                    "delta_bps": ctx.delta_bps,
                    "direction_threshold": self.delta_bps_min,
                },
            )

        # Gate 1.5: Chainlink divergence
        if (ctx.chainlink_price or 0) > 0 and ctx.open_price > 0:
            cl_delta_bps = (ctx.chainlink_price - ctx.open_price) / ctx.open_price * 10000
            if want_positive and cl_delta_bps < -self.cl_adverse_max_bps:
                return Decision(
                    action=Action.SKIP,
                    reason=(
                        f"cl_divergence cl_Δ={cl_delta_bps:+.1f}bps "
                        f"(want UP, max adverse -{self.cl_adverse_max_bps:.0f})"
                    ),
                    signal_features={
                        "side": side.value,
                        "delta_bps": ctx.delta_bps,
                        "cl_delta_bps": round(cl_delta_bps, 2),
                        "cl_price": ctx.chainlink_price,
                        "strike": ctx.open_price,
                    },
                )
            if (not want_positive) and cl_delta_bps > self.cl_adverse_max_bps:
                return Decision(
                    action=Action.SKIP,
                    reason=(
                        f"cl_divergence cl_Δ={cl_delta_bps:+.1f}bps "
                        f"(want DOWN, max adverse +{self.cl_adverse_max_bps:.0f})"
                    ),
                    signal_features={
                        "side": side.value,
                        "delta_bps": ctx.delta_bps,
                        "cl_delta_bps": round(cl_delta_bps, 2),
                        "cl_price": ctx.chainlink_price,
                        "strike": ctx.open_price,
                    },
                )

        # Gate 2: favored price cap
        fav_ask = ctx.pm_yes_ask if side is Side.YES_UP else ctx.pm_no_ask
        if not (0 < fav_ask < self.max_price):
            return Decision(
                action=Action.SKIP,
                reason=f"fav_ask {fav_ask:.3f} !< {self.max_price} ({side.value})",
                signal_features={
                    "side": side.value,
                    "fav_ask": fav_ask,
                    "max_price": self.max_price,
                },
            )

        # Gate 3: AFML filters
        spot_series = [t.spot_price for t in ctx.recent_ticks if t.spot_price > 0]
        spot_series.append(ctx.spot_price)
        feats = compute_afml_features(
            spot_series,
            pm_yes_bid=ctx.pm_yes_bid,
            pm_yes_ask=ctx.pm_yes_ask,
            pm_depth_yes=ctx.pm_depth_yes,
            pm_depth_no=ctx.pm_depth_no,
            frac_d=self.frac_d,
            frac_size=self.frac_size,
            entropy_lookback=self.entropy_lookback,
            cusum_threshold=self.cusum_threshold,
            cusum_lookback=self.cusum_lookback,
            ar_lags=self.ar_lags,
        )

        f1_fracdiff = (feats["fracdiff"] > 0) == want_positive
        ar_key = f"ar_{self.autocorr_lag}"
        f2_autocorr = feats.get(ar_key, 0) > 0
        f3_cusum = feats["cusum_event_rate"] > self.cusum_min_rate
        f4_microprice = (feats["microprice"] > 0.5) == want_positive

        # f6: recent-candle filter
        recent_spots = [
            t.spot_price
            for t in ctx.recent_ticks
            if t.spot_price > 0 and (ctx.ts - t.ts) <= self.recent_window_s
        ]
        recent_spots.append(ctx.spot_price)
        if len(recent_spots) >= 2:
            open_p = recent_spots[0]
            close_p = recent_spots[-1]
            high_p = max(recent_spots)
            low_p = min(recent_spots)
            range_p = high_p - low_p
            body_p = abs(close_p - open_p)
            body_ratio = (body_p / range_p) if range_p > 0 else 0.0
            momentum_delta = close_p - open_p
            momentum_ok = (momentum_delta > 0) == want_positive
            not_doji = body_ratio >= self.doji_body_ratio_min
            f6_candle = bool(momentum_ok and not_doji)
        else:
            body_ratio = 0.0
            momentum_ok = False
            not_doji = False
            f6_candle = False

        confirmations = sum([f1_fracdiff, f2_autocorr, f3_cusum, f4_microprice, f6_candle])

        # Monte Carlo (shadow by default — logged, not counted)
        mc_prob_up = -1.0
        f5_mc = False
        if self.mc_enabled:
            horizon_remaining = max(1, int(ctx.t_to_close))
            mc_prob_up = mc_bootstrap_prob_up(
                spot_series=spot_series,
                current_price=ctx.chainlink_price or ctx.spot_price,
                strike=ctx.open_price,
                horizon_s=horizon_remaining,
                n_sims=self.mc_n_sims,
            )
            if want_positive:
                f5_mc = mc_prob_up > self.mc_threshold
            else:
                f5_mc = mc_prob_up < (1.0 - self.mc_threshold)
            if not self.mc_shadow:
                confirmations += int(f5_mc)

        features = {
            "side": side.value,
            "delta_bps": ctx.delta_bps,
            "fav_ask": round(fav_ask, 4),
            "horizon_s": self.entry_horizon_s,
            "confirmations": confirmations,
            "min_confirmations": self.min_confirmations,
            "mc_prob_up": round(mc_prob_up, 4),
            "mc_confirms": bool(f5_mc),
            "mc_shadow": self.mc_shadow,
            "candle_momentum_ok": bool(momentum_ok),
            "candle_not_doji": bool(not_doji),
            "candle_body_ratio": round(body_ratio, 4),
            "candle_window_s": self.recent_window_s,
            "candle_n_ticks": len(recent_spots),
            **{k: round(v, 6) for k, v in feats.items()},
        }
        total_filters = 5 if self.mc_shadow else 6
        breakdown = StrategyBase.build_breakdown(
            delta_bps=round(ctx.delta_bps, 2),
            side_picked=side.value,
            fav_ask=round(fav_ask, 4),
            f1_fracdiff=f1_fracdiff,
            f2_autocorr=f2_autocorr,
            f3_cusum=f3_cusum,
            f4_microprice=f4_microprice,
            f5_mc=f"{f5_mc} (p={mc_prob_up:.3f}, shadow={self.mc_shadow})",
            f6_candle=(
                f"{f6_candle} (mom_ok={momentum_ok}, body_ratio={body_ratio:.2f}, "
                f"min={self.doji_body_ratio_min}, n={len(recent_spots)})"
            ),
            confirmations=f"{confirmations}/{total_filters}",
            min_required=self.min_confirmations,
        )

        if confirmations < self.min_confirmations:
            filter_list = [
                ("fracdiff", f1_fracdiff),
                ("autocorr", f2_autocorr),
                ("cusum", f3_cusum),
                ("microprice", f4_microprice),
                ("candle", f6_candle),
            ]
            if not self.mc_shadow:
                filter_list.append(("mc", f5_mc))
            failed = [name for name, ok in filter_list if not ok]
            return Decision(
                action=Action.SKIP,
                reason=(
                    f"confirm {confirmations}/{total_filters} < {self.min_confirmations} "
                    f"(failed: {','.join(failed)})"
                ),
                signal_features=features,
                signal_breakdown=breakdown,
            )

        # ENTER
        self._per_window_entered.add(ctx.market_slug)
        if self.order_type_str == "gtc":
            fav_bid = ctx.pm_yes_bid if side is Side.YES_UP else ctx.pm_no_bid
            mid = (
                (ctx.pm_yes_bid + ctx.pm_yes_ask) / 2
                if side is Side.YES_UP
                else (ctx.pm_no_bid + ctx.pm_no_ask) / 2
            )
            offset = (self.limit_offset_bps / 10000.0) * max(mid, 0.001)
            target = min(fav_ask - offset, fav_bid) if fav_bid > 0 else (fav_ask - offset)
            limit_price = max(0.01, round(target, 3))
            return Decision(
                action=Action.ENTER,
                side=side,
                limit_price=limit_price,
                reason=(
                    f"trend_confirm Δ={ctx.delta_bps:+.1f} "
                    f"confirm={confirmations}/{total_filters} "
                    f"fav={fav_ask:.3f} gtc@{limit_price:.3f}"
                ),
                signal_features=features,
                signal_breakdown=breakdown,
                order_type=OrderType.GTC,
                ttl_seconds=self.gtc_ttl_seconds,
                horizon_s=self.entry_horizon_s,
            )
        return Decision(
            action=Action.ENTER,
            side=side,
            limit_price=fav_ask,
            reason=(
                f"trend_confirm Δ={ctx.delta_bps:+.1f} "
                f"confirm={confirmations}/{total_filters} fav={fav_ask:.3f} market"
            ),
            signal_features=features,
            signal_breakdown=breakdown,
            order_type=OrderType.MARKET,
            horizon_s=self.entry_horizon_s,
        )

    def notify_window_rollover(self, new_slug: str) -> None:
        self._per_window_entered.clear()
