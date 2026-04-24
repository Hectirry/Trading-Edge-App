"""Shared base class for grid-trading strategies (ADR 3.8a).

Owns
----
* grid level computation (arithmetic or geometric step sizing)
* deterministic client_order_id generation via ``deterministic_coid``
* realized-PnL bookkeeping across reset generations
* stop-loss gate (absolute % below initial center — NOT trailing, per plan)
* pair-offset placement on fill (BUY fill → place opposite SELL above, etc.)

Subclasses implement policy (when/how to reset, whether to gate by
regime, how to size steps). ``GridBase.on_trade_tick`` handles the
stop-loss check only; subclasses that override it must call
``super().on_trade_tick`` and merge the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading.engine.continuous_strategy_base import (
    Action,
    Bar,
    CancelAll,
    ContinuousStrategyBase,
    Place,
)
from trading.paper.limit_book_sim import LimitFill, LimitOrder, deterministic_coid


@dataclass
class GridLevel:
    idx: int
    side: str  # "BUY" or "SELL"
    price: float


@dataclass
class GridState:
    reset_gen: int = 0
    center_price: float = 0.0
    realized_pnl: float = 0.0
    initial_center: float = 0.0
    stopped_out: bool = False
    last_fill_by_level: dict[int, str] = field(default_factory=dict)


def compute_levels(
    *,
    center: float,
    step: float,
    n_below: int,
    n_above: int,
    geometric: bool = False,
) -> list[GridLevel]:
    """N_below BUY levels below center, N_above SELL levels above.

    Arithmetic: price_k = center ± k * step (step in absolute units)
    Geometric:  price_k = center * (1 ± step)**k (step as fraction)

    Level ``idx`` is positive upwards (SELL 1..N_above) and negative
    downwards (BUY -1..-N_below); level 0 is the center (no order).
    """
    if center <= 0 or step <= 0 or n_below < 0 or n_above < 0:
        raise ValueError("center, step must be > 0; n_* must be >= 0")
    levels: list[GridLevel] = []
    for k in range(1, n_below + 1):
        price = center - k * step if not geometric else center * (1 - step) ** k
        if price > 0:
            levels.append(GridLevel(idx=-k, side="BUY", price=price))
    for k in range(1, n_above + 1):
        price = center + k * step if not geometric else center * (1 + step) ** k
        levels.append(GridLevel(idx=k, side="SELL", price=price))
    return levels


class GridBase(ContinuousStrategyBase):
    """Concrete grid strategies extend this and override:

    * ``_initial_center`` — center at start
    * ``_grid_levels``    — compute levels given current state
    * (optional) ``on_trade_tick`` / ``on_bar_1m`` — policy checks (reset, HMM gate)
    """

    name: str = "grid-base"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        p = self.params
        self.qty_per_level: float = float(p["qty_per_level"])
        self.n_below: int = int(p.get("n_below", 15))
        self.n_above: int = int(p.get("n_above", 15))
        self.step: float = float(p["step"])
        self.geometric: bool = bool(p.get("geometric", False))
        self.stop_loss_pct: float = float(p.get("stop_loss_pct", 0.15))
        self.ttl_s: float | None = p.get("order_ttl_s")
        self.state = GridState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, *, spot_px: float, ts: float) -> list[Action]:
        center = self._initial_center(spot_px=spot_px)
        self.state.center_price = center
        self.state.initial_center = center
        return self._place_grid_actions(center=center, ts=ts)

    def on_trade_tick(self, *, px: float, ts: float) -> list[Action]:
        if self.state.stopped_out:
            return []
        # Absolute stop-loss floor from initial_center (see plan).
        lower_stop = self.state.initial_center * (1 - self.stop_loss_pct)
        if px <= lower_stop:
            self.state.stopped_out = True
            return [CancelAll(reason=f"stop_loss_{self.stop_loss_pct:.3f}")]
        return []

    def on_bar_1m(self, bar: Bar) -> list[Action]:
        return []

    def on_fill(self, fill: LimitFill) -> list[Action]:
        # Initial placement already holds both sides of every level; a
        # fill simply consumes one leg. Round-trip PnL is computed at
        # report time by pairing BUY / SELL fills (FIFO) — no mid-cycle
        # replenishment, which would collide with still-open opposite
        # orders and would produce ``duplicate_coid`` warnings for no
        # economic reason.
        self.state.last_fill_by_level[self._fill_level_idx(fill)] = fill.side
        return []

    # ------------------------------------------------------------------
    # Helpers for subclasses / tests
    # ------------------------------------------------------------------

    def _initial_center(self, *, spot_px: float) -> float:
        explicit = self.params.get("center_price")
        return float(explicit) if explicit else float(spot_px)

    def _grid_levels(self, center: float) -> list[GridLevel]:
        return compute_levels(
            center=center,
            step=self.step,
            n_below=self.n_below,
            n_above=self.n_above,
            geometric=self.geometric,
        )

    def _place_grid_actions(
        self,
        *,
        center: float,
        ts: float,
        reset_gen: int | None = None,
    ) -> list[Action]:
        """Build a Place action per grid level.

        ``reset_gen`` defaults to the strategy's current generation. DGT-style
        subclasses that emit ``Reset`` + ``Place`` in the same batch must pass
        the *post-reset* generation explicitly, because the driver bumps
        ``self.state.reset_gen`` only after processing the Reset action — the
        Place actions would otherwise embed stale coids.
        """
        gen = self.state.reset_gen if reset_gen is None else reset_gen
        actions: list[Action] = []
        for lvl in self._grid_levels(center):
            coid = deterministic_coid(
                strategy_id=self.strategy_id,
                instrument_id=self.instrument_id,
                reset_gen=gen,
                level_idx=lvl.idx,
                side=lvl.side,
                center_price=center,
            )
            order = LimitOrder(
                coid=coid,
                strategy_id=self.strategy_id,
                instrument_id=self.instrument_id,
                side=lvl.side,
                price=lvl.price,
                qty=self.qty_per_level,
                ts_placed=ts,
                ttl_s=self.ttl_s,
                metadata={"level_idx": lvl.idx, "reset_gen": gen},
            )
            actions.append(Place(order=order))
        return actions

    def _fill_level_idx(self, fill: LimitFill) -> int:
        # Level idx is encoded in metadata when placed, but fills don't
        # carry metadata — infer from price proximity to the grid.
        for lvl in self._grid_levels(self.state.center_price):
            if abs(lvl.price - fill.price) < 1e-8 and lvl.side == fill.side:
                return lvl.idx
        return 0

    def on_reset_applied(
        self, *, old_center: float, new_center: float, realized_pnl: float, ts: float
    ) -> None:
        self.state.reset_gen += 1
        self.state.center_price = new_center
        self.state.realized_pnl = realized_pnl

    # Convenience for tests + dashboards.
    @property
    def realized_pnl(self) -> float:
        return self.state.realized_pnl

    @property
    def center_price(self) -> float:
        return self.state.center_price
