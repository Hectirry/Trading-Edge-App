"""grid_dgt_v1 — Dynamic Grid Trading (Chen et al. 2025, arXiv 2506.11921).

Static grid has zero expected return under random walk; DGT fixes that by
resetting the grid when spot exceeds either boundary. On breach:

1. Snapshot realized PnL (driver cancels all outstanding levels)
2. Re-center the grid at the current spot
3. Rebuild with the same geometry

Keeps maker exposure through trending moves while preserving round-trip
spread capture within each regime.
"""

from __future__ import annotations

from trading.engine.continuous_strategy_base import Action, Reset
from trading.strategies.grid.grid_base import GridBase


class GridDgtV1(GridBase):
    name: str = "grid_dgt_v1"

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

        new_center = px
        next_gen = self.state.reset_gen + 1
        reason = "breach_upper" if px > upper else "breach_lower"
        actions: list[Action] = [Reset(new_center=new_center, reason=reason)]
        actions.extend(self._place_grid_actions(center=new_center, ts=ts, reset_gen=next_gen))
        return actions
