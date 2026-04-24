"""Shared base class for grid-trading strategies (ADR 3.8a / 3.8a.1).

Owns
----
* grid level computation (arithmetic or geometric step sizing)
* deterministic ``client_order_id`` generation via ``deterministic_coid``
* spot-valid initial placement: BUY-only mode (``buy_only=True``) emits
  only BUYs below center at start — the SELL leg of each pair is posted
  by ``on_fill`` at the mirrored level. Symmetric mode keeps the legacy
  behaviour (BUYs + SELLs placed at start) for reference / tests.
* stop-loss gate anchored to ``state.center_price`` (not ``initial_center``)
  so DGT resets move the floor with the new centre.
* round-trip realised-PnL bookkeeping.

Sprint 3.8a.1 fixes:
    1. ``buy_only`` flag removes phantom-short SELLs at start.
    2. SL uses current centre_price, not initial_centre.
    3. Paired SELL on BUY fill emits at level +k (mirrored on current
       centre) when ``buy_only=True``.
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
    # Tracks open SELL orders by level_idx so we don't double-place when
    # a BUY re-fills on a second trip through the same level.
    open_sells_by_level: set[int] = field(default_factory=set)


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
    * (optional) ``on_trade_tick`` / ``on_bar_1m`` — policy checks (reset,
      regime gate)
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
        # Spot-valid default: only BUYs below center at start; SELLs are
        # emitted by ``on_fill`` when their paired BUY fills.
        self.buy_only: bool = bool(p.get("buy_only", True))
        # Offset from the filled BUY level at which to post the paired
        # SELL. 1 → post SELL at level +1 (center + 1 step). 2 → +2 step.
        self.pair_sell_offset_levels: int = int(p.get("pair_sell_offset_levels", 1))
        self.ttl_s: float | None = p.get("order_ttl_s")
        self.state = GridState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, *, spot_px: float, ts: float) -> list[Action]:
        center = self._initial_center(spot_px=spot_px)
        self.state.center_price = center
        self.state.initial_center = center
        return self._place_grid_actions(
            center=center,
            ts=ts,
            sides=("BUY",) if self.buy_only else ("BUY", "SELL"),
        )

    def on_trade_tick(self, *, px: float, ts: float) -> list[Action]:
        if self.state.stopped_out:
            return []
        # Stop-loss anchored to the *current* centre_price so DGT-style
        # resets move the floor with the grid. Prevents the 3.8a bug where
        # SL remained anchored to initial_center while grid reset
        # repeatedly lower — immediate SL trigger post-reset.
        lower_stop = self.state.center_price * (1 - self.stop_loss_pct)
        if px <= lower_stop:
            self.state.stopped_out = True
            return [CancelAll(reason=f"stop_loss_{self.stop_loss_pct:.3f}")]
        return []

    def on_bar_1m(self, bar: Bar) -> list[Action]:
        return []

    def on_fill(self, fill: LimitFill) -> list[Action]:
        level = self._fill_level_idx(fill)
        self.state.last_fill_by_level[level] = fill.side
        if not self.buy_only or fill.side != "BUY" or level >= 0:
            # Symmetric mode: initial placement held both legs, no action.
            # Sell fill in buy-only mode: round-trip complete, done.
            if fill.side == "SELL":
                self.state.open_sells_by_level.discard(level)
            return []

        # BUY fill in buy-only mode → post paired SELL at mirrored level.
        target_idx = abs(level) + (self.pair_sell_offset_levels - 1)
        if target_idx in self.state.open_sells_by_level:
            # Already have a SELL open at this target (prior round-trip).
            return []
        price = self._level_price(
            center=self.state.center_price,
            idx=target_idx,
            side="SELL",
        )
        if price is None:
            return []
        coid = deterministic_coid(
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            reset_gen=self.state.reset_gen,
            level_idx=target_idx,
            side="SELL",
            center_price=self.state.center_price,
        )
        order = LimitOrder(
            coid=coid,
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            side="SELL",
            price=price,
            qty=self.qty_per_level,
            ts_placed=fill.ts,
            ttl_s=self.ttl_s,
            metadata={
                "level_idx": target_idx,
                "reset_gen": self.state.reset_gen,
                "paired_with": fill.coid,
            },
        )
        self.state.open_sells_by_level.add(target_idx)
        return [Place(order=order)]

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

    def _level_price(self, *, center: float, idx: int, side: str) -> float | None:
        """Return the price at level ``idx`` (positive = above, negative = below)
        using the strategy's current step / geometry. ``None`` if the computed
        price would be non-positive or beyond the configured band."""
        if idx == 0:
            return None
        k = abs(idx)
        if idx > 0 and k > self.n_above:
            return None
        if idx < 0 and k > self.n_below:
            return None
        if not self.geometric:
            price = center + idx * self.step if idx > 0 else center + idx * self.step
        else:
            factor = (1 + self.step) ** k if idx > 0 else (1 - self.step) ** k
            price = center * factor
        return price if price > 0 else None

    def _place_grid_actions(
        self,
        *,
        center: float,
        ts: float,
        reset_gen: int | None = None,
        sides: tuple[str, ...] = ("BUY", "SELL"),
    ) -> list[Action]:
        """Build a Place action per grid level, filtered by ``sides``.

        ``reset_gen`` defaults to the strategy's current generation. DGT-style
        subclasses that emit ``Reset`` + ``Place`` in the same batch must pass
        the *post-reset* generation explicitly, because the driver bumps
        ``self.state.reset_gen`` only after processing the Reset action — the
        Place actions would otherwise embed stale coids.

        ``sides`` restricts which legs get placed. Default is both legs
        (legacy / symmetric mode); ``buy_only=True`` flows call with
        ``sides=("BUY",)``.
        """
        gen = self.state.reset_gen if reset_gen is None else reset_gen
        actions: list[Action] = []
        for lvl in self._grid_levels(center):
            if lvl.side not in sides:
                continue
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
            if abs(lvl.price - fill.price) < 1e-6 and lvl.side == fill.side:
                return lvl.idx
        return 0

    def on_reset_applied(
        self, *, old_center: float, new_center: float, realized_pnl: float, ts: float
    ) -> None:
        self.state.reset_gen += 1
        self.state.center_price = new_center
        self.state.realized_pnl = realized_pnl
        # New generation starts with no paired SELLs open.
        self.state.open_sells_by_level.clear()

    # Convenience for tests + dashboards.
    @property
    def realized_pnl(self) -> float:
        return self.state.realized_pnl

    @property
    def center_price(self) -> float:
        return self.state.center_price
