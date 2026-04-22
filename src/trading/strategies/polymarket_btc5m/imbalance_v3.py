"""
imbalance_v3 — port from /home/coder/polybot-btc5m/strategies/imbalance_v3.py.

Port is literal in behavior per the Phase 2 plan. Filters, thresholds, and
order of evaluation match the source. Only structural changes:
  - StrategyBase shim (src/trading/engine/strategy_base.py) replaces the
    polybot core.strategy_base import.
  - Regime lookup is a pluggable callable (injected) rather than a direct
    SQLite query, so parity tests can feed the regime fixture.

Hypothesis (from polybot-btc5m README): exploit Polymarket orderbook
imbalances plus a Black-Scholes mispricing edge, with size throttled
by depth and vol regime. v3 adds streak protection and an optional
regime filter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext


class ImbalanceV3(StrategyBase):
    name = "imbalance_v3"

    def __init__(
        self,
        config: dict,
        regime_lookup: Callable[[float], str] | None = None,
    ) -> None:
        super().__init__(config)
        self.consecutive_losses: int = 0
        self.streak_pause_until: float = 0.0
        self.last_resolution_ts: float = 0.0
        self._regime_lookup = regime_lookup

    def on_trade_resolved(self, resolution: str, pnl: float, ts: float) -> None:
        self.last_resolution_ts = ts
        if resolution == "loss":
            self.consecutive_losses += 1
            max_streak = int(self.params.get("max_consecutive_losses", 3))
            if self.consecutive_losses >= max_streak:
                pause_min = int(self.params.get("streak_pause_minutes", 30))
                self.streak_pause_until = ts + pause_min * 60
        elif resolution == "win":
            self.consecutive_losses = 0

    def should_enter(self, ctx: TickContext) -> Decision:
        p = self.params

        # v3-specific filter: streak pause.
        if ctx.ts < self.streak_pause_until:
            remaining = int(self.streak_pause_until - ctx.ts)
            return Decision(
                action=Action.SKIP,
                reason=f"v3 streak_pause {remaining}s (losses={self.consecutive_losses})",
                signal_features={"consecutive_losses": self.consecutive_losses},
            )

        # v3-specific filter: regime block (optional).
        blocked_regimes = set(p.get("blocked_regimes") or [])
        if blocked_regimes and self._regime_lookup is not None:
            regime = self._regime_lookup(ctx.ts)
            if regime in blocked_regimes:
                return Decision(
                    action=Action.SKIP,
                    reason=f"v3 régimen {regime} bloqueado",
                    signal_features={"regime": regime},
                )

        # v2-inherited filters.
        threshold = p.get("imbalance_threshold", 1.05)
        min_depth_total = p.get("min_depth_total_usd", 300.0)
        max_spread_bps = p.get("max_spread_bps", 200.0)
        depth_trend_min_pct = p.get("require_depth_trend_min_pct", -5.0)
        allowed_sides = set(p.get("allowed_sides") or ["YES_UP"])
        blocked_hours = set(p.get("blocked_hours_utc") or [])

        depth_total = ctx.pm_depth_yes + ctx.pm_depth_no
        imb = ctx.pm_imbalance
        imb_inv = (1 / imb) if imb > 0 else 0.0
        hour_utc = datetime.fromtimestamp(ctx.ts, tz=UTC).hour

        depth_trend_pct: float | None = None
        if ctx.recent_ticks:
            window = ctx.recent_ticks[-30:] if len(ctx.recent_ticks) >= 30 else ctx.recent_ticks
            if window:
                first = window[0]
                d_then = (first.pm_depth_yes or 0) + (first.pm_depth_no or 0)
                if d_then > 0:
                    depth_trend_pct = (depth_total - d_then) / d_then * 100

        features = {
            "pm_imbalance": imb,
            "depth_total": depth_total,
            "spread_bps": ctx.pm_spread_bps,
            "depth_trend_pct": depth_trend_pct,
            "hour_utc": hour_utc,
            "consecutive_losses": self.consecutive_losses,
            "z_score": ctx.z_score,
            "edge": ctx.edge,
        }

        if hour_utc in blocked_hours:
            return Decision(
                action=Action.SKIP,
                reason=f"v3 hora {hour_utc:02d}h bloqueada",
                signal_features=features,
            )
        if ctx.pm_spread_bps > max_spread_bps:
            return Decision(
                action=Action.SKIP,
                reason=f"v3 spread {ctx.pm_spread_bps:.0f} > {max_spread_bps:.0f}",
                signal_features=features,
            )
        if depth_total < min_depth_total:
            return Decision(
                action=Action.SKIP,
                reason=f"v3 depth ${depth_total:.0f} < ${min_depth_total:.0f}",
                signal_features=features,
            )
        if depth_trend_pct is not None and depth_trend_pct < depth_trend_min_pct:
            return Decision(
                action=Action.SKIP,
                reason=f"v3 depth_trend {depth_trend_pct:+.1f}% < {depth_trend_min_pct:+.1f}%",
                signal_features=features,
            )

        # Entry logic (same precedence as v3).
        if imb >= threshold:
            if "YES_UP" not in allowed_sides:
                return Decision(
                    action=Action.SKIP,
                    reason="v3 YES_UP deshabilitado",
                    signal_features=features,
                )
            return Decision(
                action=Action.ENTER,
                side=Side.YES_UP,
                reason=(
                    f"v3: imb={imb:.2f}, depth=${depth_total:.0f}, "
                    f"spread={ctx.pm_spread_bps:.0f}bps"
                ),
                signal_features=features,
            )
        if imb > 0 and imb_inv >= threshold:
            if "YES_DOWN" not in allowed_sides:
                return Decision(
                    action=Action.SKIP,
                    reason="v3 YES_DOWN deshabilitado",
                    signal_features=features,
                )
            return Decision(
                action=Action.ENTER,
                side=Side.YES_DOWN,
                reason=f"v3: imb_inv={imb_inv:.2f}",
                signal_features=features,
            )

        return Decision(
            action=Action.SKIP,
            side=Side.NONE,
            reason=f"v3 imbalance neutral ({imb:.2f})",
            signal_features=features,
        )
