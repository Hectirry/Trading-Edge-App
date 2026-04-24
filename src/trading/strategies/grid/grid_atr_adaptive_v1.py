"""grid_atr_adaptive_v1 — ATR-sized grid step (Phase 3.8a / 3.8a.1).

Step size tracks realised range instead of a fixed fraction: ``step =
ATR(period) * multiplier``.

Sprint 3.8a.1 adjustments (learning from the catastrophic 1m-bar
churn seen in the initial backtest):

* ``atr_bar_window_s`` aggregates 1m inputs into synthetic bars of
  the given window (default 900 = 15 m). This avoids the 17-second
  rebuild cadence produced by raw 1m ATR.
* ``atr_multiplier`` default raised 0.5 → 2.0 so the step is wider
  than a typical single-bar range (the grid absorbs noise, captures
  expansion).
* ``recompute_delta_pct`` default raised 0.20 → 0.40 — require 40%
  deviation from the last-applied step before rebuilding.
* ``rebuild_cooldown_s`` (default 3600) enforces at least 1 hour
  between rebuilds.
* ``buy_only`` default True (inherited) → rebuild places BUY-only;
  SELLs paired on fill.
"""

from __future__ import annotations

from collections import deque

from trading.engine.continuous_strategy_base import Action, Bar, Reset
from trading.engine.features.atr import ATR
from trading.strategies.grid.grid_base import GridBase


class GridAtrAdaptiveV1(GridBase):
    name: str = "grid_atr_adaptive_v1"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        p = self.params
        self.atr_period: int = int(p.get("atr_period", 14))
        self.atr_multiplier: float = float(p.get("atr_multiplier", 2.0))
        self.recompute_delta_pct: float = float(p.get("recompute_delta_pct", 0.40))
        self.atr_bar_window_s: float = float(p.get("atr_bar_window_s", 900.0))
        self.rebuild_cooldown_s: float = float(p.get("rebuild_cooldown_s", 3600.0))
        # 3.8a.2 trend gate — skip rebuild when |1h return| exceeds the
        # threshold so strong directional moves don't keep replacing the
        # grid at lower/higher prices (only to see it breach again).
        self.trend_gate_1h_pct: float = float(p.get("trend_gate_1h_pct", 0.0))
        self._closes_1h: deque[float] = deque(maxlen=60)
        self._atr = ATR(period=self.atr_period)
        self._last_step: float = float(self.step)
        self._last_rebuild_ts: float = 0.0
        # Synthetic bar aggregation state.
        self._agg_open_ts: float | None = None
        self._agg_open: float = 0.0
        self._agg_high: float = 0.0
        self._agg_low: float = 0.0
        self._agg_close: float = 0.0

    def _maybe_emit_aggregate(self, bar: Bar) -> Bar | None:
        """Aggregate incoming 1m bars into a bar of length
        ``atr_bar_window_s``. Return the completed aggregate bar when the
        window closes, otherwise None.

        Pass-through when ``atr_bar_window_s <= 60`` (no aggregation, one
        output per 1m input) — used by tests that drive tight sequences.
        """
        if self.atr_bar_window_s <= 60.0:
            return bar
        if self._agg_open_ts is None:
            self._agg_open_ts = bar.ts_open
            self._agg_open = bar.open
            self._agg_high = bar.high
            self._agg_low = bar.low
            self._agg_close = bar.close
            return None
        self._agg_high = max(self._agg_high, bar.high)
        self._agg_low = min(self._agg_low, bar.low)
        self._agg_close = bar.close
        if bar.ts_close - self._agg_open_ts >= self.atr_bar_window_s:
            agg = Bar(
                ts_open=self._agg_open_ts,
                ts_close=bar.ts_close,
                open=self._agg_open,
                high=self._agg_high,
                low=self._agg_low,
                close=self._agg_close,
                volume=0.0,
            )
            self._agg_open_ts = None
            return agg
        return None

    def _trending_too_hard(self) -> bool:
        if self.trend_gate_1h_pct <= 0 or len(self._closes_1h) < 60:
            return False
        first = self._closes_1h[0]
        last = self._closes_1h[-1]
        if first <= 0:
            return False
        return abs(last - first) / first > self.trend_gate_1h_pct

    def on_bar_1m(self, bar: Bar) -> list[Action]:
        self._closes_1h.append(bar.close)
        agg = self._maybe_emit_aggregate(bar)
        if agg is None:
            return []
        atr_now = self._atr.update(agg.high, agg.low, agg.close)
        if not self._atr.ready or atr_now <= 0:
            return []
        new_step = atr_now * self.atr_multiplier
        if new_step <= 0:
            return []
        delta = abs(new_step - self._last_step) / max(self._last_step, 1e-9)
        if delta < self.recompute_delta_pct:
            return []
        if (
            self.rebuild_cooldown_s > 0
            and (agg.ts_close - self._last_rebuild_ts) < self.rebuild_cooldown_s
        ):
            return []
        if self._trending_too_hard():
            return []
        self.step = new_step
        self._last_step = new_step
        self._last_rebuild_ts = agg.ts_close
        next_gen = self.state.reset_gen + 1
        center = self.state.center_price
        actions: list[Action] = [Reset(new_center=center, reason="atr_shift")]
        actions.extend(
            self._place_grid_actions(
                center=center,
                ts=agg.ts_close,
                reset_gen=next_gen,
                sides=("BUY",) if self.buy_only else ("BUY", "SELL"),
            )
        )
        return actions
