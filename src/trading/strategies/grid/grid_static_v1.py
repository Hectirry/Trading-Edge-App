"""grid_static_v1 — fixed-range static grid (ADR 3.8a smoke).

Baseline for comparing DGT / ATR-adaptive / regime-gated variants in
3.8b. No reset, no regime gate, no ATR: places N_below + N_above limit
orders once at ``on_start`` and re-posts the opposite side on every
fill. Stop-loss at ``stop_loss_pct`` below initial center cancels all
and halts further placement (the strategy is effectively dead after a
stop — acceptable for this baseline).
"""

from __future__ import annotations

from trading.strategies.grid.grid_base import GridBase


class GridStaticV1(GridBase):
    name: str = "grid_static_v1"
