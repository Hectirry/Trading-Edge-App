"""grid_atr_adaptive_v1 — ATR-sized grid step (Phase 3.8a).

Step size tracks realized range instead of a fixed fraction: ``step =
ATR(period) * multiplier``. ATR updates on each 1m bar; the grid is
rebuilt when the new step deviates from the last applied step by more
than ``recompute_delta_pct`` (default 20%), avoiding order-churn from
tiny ATR wobble.

Rebuild is a reset around the *current* center (not spot). Breach-based
reset (DGT) is intentionally left to ``grid_dgt_v1`` — combining both
policies is 3.8b territory.
"""

from __future__ import annotations

from trading.engine.continuous_strategy_base import Action, Bar, Reset
from trading.engine.features.atr import ATR
from trading.strategies.grid.grid_base import GridBase


class GridAtrAdaptiveV1(GridBase):
    name: str = "grid_atr_adaptive_v1"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        p = self.params
        self.atr_period: int = int(p.get("atr_period", 14))
        self.atr_multiplier: float = float(p.get("atr_multiplier", 0.5))
        self.recompute_delta_pct: float = float(p.get("recompute_delta_pct", 0.20))
        self._atr = ATR(period=self.atr_period)
        # Seeded from config; first ATR-driven resize replaces it.
        self._last_step: float = float(self.step)

    def on_bar_1m(self, bar: Bar) -> list[Action]:
        atr_now = self._atr.update(bar.high, bar.low, bar.close)
        if not self._atr.ready or atr_now <= 0:
            return []

        new_step = atr_now * self.atr_multiplier
        if new_step <= 0:
            return []

        delta = abs(new_step - self._last_step) / max(self._last_step, 1e-9)
        if delta < self.recompute_delta_pct:
            return []

        self.step = new_step
        self._last_step = new_step
        next_gen = self.state.reset_gen + 1
        center = self.state.center_price
        actions: list[Action] = [Reset(new_center=center, reason="atr_shift")]
        actions.extend(self._place_grid_actions(center=center, ts=bar.ts_close, reset_gen=next_gen))
        return actions
