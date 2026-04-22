"""RiskManager port — see /home/coder/polybot-btc5m/core/risk.py.

Gates strategy entries by cooldown, daily loss limit, rolling loss window,
spread/depth/edge/z-score thresholds. Used in both backtest mode (with
ctx.ts as the replay clock) and paper/live (where ctx.ts ≈ wall clock)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RiskState:
    cooldown_until: float = 0.0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    day_key: str = ""
    circuit_breaker_tripped: bool = False
    trip_reason: str = ""
    recent_pnls: deque = field(default_factory=deque)
    pause_until: float = 0.0
    pause_reason: str = ""


class RiskManager:
    def __init__(self, cfg: dict) -> None:
        r = cfg["risk"]
        self.cooldown_seconds = int(r["cooldown_seconds"])
        self.max_size = float(r["max_position_size_usd"])
        self.daily_loss_limit = float(r["daily_loss_limit_usd"])
        self.daily_trade_limit = int(r["daily_trade_limit"])
        self.min_edge_bps = float(r["min_edge_bps"])
        self.min_z_score = float(r["min_z_score"])
        self.min_depth = float(r["min_pm_depth_usd"])
        self.max_spread = float(r["skip_if_spread_bps"])
        self.loss_pause_threshold = float(r.get("loss_pause_threshold_usd", 0))
        self.loss_pause_window_s = int(r.get("loss_pause_window_minutes", 30)) * 60
        self.loss_pause_duration_s = int(r.get("loss_pause_duration_minutes", 30)) * 60
        self.state = RiskState(day_key=self._today(0.0))

    @staticmethod
    def _today(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")

    def _roll_day(self, now: float) -> None:
        today = self._today(now)
        if today != self.state.day_key:
            self.state = RiskState(day_key=today)

    def can_enter(self, ctx) -> tuple[bool, str]:
        now = float(ctx.ts)
        self._roll_day(now)
        if self.state.circuit_breaker_tripped:
            return False, f"circuit_breaker: {self.state.trip_reason}"
        if now < self.state.pause_until:
            remaining = int(self.state.pause_until - now)
            return False, f"cool-off rodante {remaining}s restantes ({self.state.pause_reason})"
        if now < self.state.cooldown_until:
            return False, f"cooldown {int(self.state.cooldown_until - now)}s restantes"
        if self.state.daily_trades >= self.daily_trade_limit:
            return False, f"límite de trades diarios ({self.daily_trade_limit})"
        if self.state.daily_pnl <= -self.daily_loss_limit:
            self.state.circuit_breaker_tripped = True
            self.state.trip_reason = f"daily_loss_limit hit: {self.state.daily_pnl:.2f}"
            return False, self.state.trip_reason
        if ctx.pm_spread_bps > self.max_spread:
            return False, f"spread PM muy ancho ({ctx.pm_spread_bps:.0f} bps)"
        depth_needed = min(ctx.pm_depth_yes, ctx.pm_depth_no)
        if depth_needed < self.min_depth:
            return False, f"depth bajo (${depth_needed:.1f} < ${self.min_depth})"
        if abs(ctx.z_score) < self.min_z_score:
            return False, f"z_score bajo ({ctx.z_score:.2f} < {self.min_z_score})"
        if abs(ctx.edge) * 10000 < self.min_edge_bps:
            return False, f"edge bajo ({abs(ctx.edge) * 10000:.0f} bps)"
        return True, "ok"

    def on_trade_closed(self, pnl: float, now: float) -> None:
        self._roll_day(now)
        self.state.daily_pnl += pnl
        self.state.daily_trades += 1
        self.state.cooldown_until = now + self.cooldown_seconds
        if self.loss_pause_threshold > 0:
            self.state.recent_pnls.append((now, pnl))
            cutoff = now - self.loss_pause_window_s
            while self.state.recent_pnls and self.state.recent_pnls[0][0] < cutoff:
                self.state.recent_pnls.popleft()
            window_pnl = sum(p for _, p in self.state.recent_pnls)
            if window_pnl <= -self.loss_pause_threshold:
                self.state.pause_until = now + self.loss_pause_duration_s
                self.state.pause_reason = f"rolling loss ${window_pnl:.2f}"
                self.state.recent_pnls.clear()
