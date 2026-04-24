"""grid_dgt_v1 — Dynamic Grid Trading (Chen et al. 2025, arXiv 2506.11921).

Sprint 3.8a.1 updates:
    * ``buy_only`` (inherited default True) → post-reset placement is
      BUY-only; paired SELLs are emitted by ``on_fill``.
    * ``reset_cooldown_s`` — minimum gap between resets to prevent
      back-to-back cancellations in trending markets.
    * Optional trend-gate via ``trend_gate_1h_pct`` — skip reset (and
      pause placements) when |1h return| exceeds the threshold.

On boundary breach (spot outside the outer grid levels) without
cooldown violation and without trend-gate block:

  1. snapshot realised PnL (driver cancels all outstanding levels)
  2. re-centre the grid at the current spot
  3. rebuild BUY-only (SELLs posted on fill)
"""

from __future__ import annotations

from collections import deque

from trading.engine.continuous_strategy_base import Action, Bar, Reset
from trading.strategies.grid.grid_base import GridBase


class GridDgtV1(GridBase):
    name: str = "grid_dgt_v1"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        p = self.params
        self.reset_cooldown_s: float = float(p.get("reset_cooldown_s", 0.0))
        self.trend_gate_1h_pct: float = float(p.get("trend_gate_1h_pct", 0.0))
        self._last_reset_ts: float = 0.0
        # Rolling 60-bar (1 h) deque of 1-minute closes; pushed from
        # ``on_bar_1m``. Consumed by ``_trending_too_hard``.
        self._closes_1h: deque[float] = deque(maxlen=60)

    def on_bar_1m(self, bar: Bar) -> list[Action]:
        self._closes_1h.append(bar.close)
        return []

    def _trending_too_hard(self) -> bool:
        if self.trend_gate_1h_pct <= 0 or len(self._closes_1h) < 60:
            return False
        first = self._closes_1h[0]
        last = self._closes_1h[-1]
        if first <= 0:
            return False
        ret = abs(last - first) / first
        return ret > self.trend_gate_1h_pct

    def on_trade_tick(self, *, px: float, ts: float) -> list[Action]:
        stop = super().on_trade_tick(px=px, ts=ts)
        if stop:
            return stop
        if self.state.stopped_out:
            return []

        levels = self._grid_levels(self.state.center_price)
        if not levels:
            return []
        upper = max(lvl.price for lvl in levels)
        lower = min(lvl.price for lvl in levels)
        if lower <= px <= upper:
            return []

        # Cooldown gate
        if self.reset_cooldown_s > 0 and (ts - self._last_reset_ts) < self.reset_cooldown_s:
            return []

        # Trend gate — strong 1h moves typically mean price won't revert
        # back to the new centre; a reset would keep chasing without mean
        # reversion. Skip and wait for cool-down.
        if self._trending_too_hard():
            return []

        new_center = px
        next_gen = self.state.reset_gen + 1
        reason = "breach_upper" if px > upper else "breach_lower"
        actions: list[Action] = [Reset(new_center=new_center, reason=reason)]
        actions.extend(
            self._place_grid_actions(
                center=new_center,
                ts=ts,
                reset_gen=next_gen,
                sides=("BUY",) if self.buy_only else ("BUY", "SELL"),
            )
        )
        self._last_reset_ts = ts
        return actions
