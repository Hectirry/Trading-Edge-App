"""Continuous-strategy driver (ADR 3.8a).

Separate from the Polymarket-oriented ``paper/driver.py``: continuous
strategies (grid trading) keep N open limit orders and react to tick /
bar streams rather than deciding once per tick. This driver owns a
``LimitBookSim`` per strategy and applies the ``Action`` sequence the
strategy returns against it.

Design
------
Pure apply logic (``apply_actions``) is separated from the async run
loop so unit tests can drive strategies without Redis or asyncio
pubsub. ``ContinuousDriver.run_with_iterator`` accepts an async
iterator of ``(instrument_id, px, ts)`` tuples — useful for backtests
and integration tests; the Redis fanout is a thin wrapper on top.

Safety
------
Reset is atomic: all outstanding orders for the strategy are cancelled
first (``cancel_all``), realized PnL is computed by the strategy itself
before the cancel batch, and only then is the new grid placed. The
driver does not retry failed operations; if persistence fails the
LimitBookSim still serves the in-memory view correctly, and a later
reconciler job will fix the DB state (out of scope for 3.8a).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from trading.common.logging import get_logger
from trading.engine.continuous_strategy_base import (
    Action,
    Bar,
    Cancel,
    CancelAll,
    ContinuousStrategyBase,
    Place,
    Reset,
)
from trading.paper.exec_client import SimulatedExecutionClient
from trading.paper.limit_book_sim import LimitBookSim

log = get_logger(__name__)


@dataclass
class ContinuousDriverStats:
    ticks: int = 0
    fills: int = 0
    placed: int = 0
    cancelled: int = 0
    resets: int = 0
    errors: int = 0
    last_px: float | None = field(default=None)
    last_ts: float | None = field(default=None)


class ContinuousDriver:
    def __init__(
        self,
        *,
        strategy: ContinuousStrategyBase,
        book: LimitBookSim,
    ) -> None:
        self.strategy = strategy
        self.book = book
        self.stats = ContinuousDriverStats()
        self._started = False

    async def start(self, *, spot_px: float, ts: float) -> None:
        if self._started:
            return
        actions = self.strategy.on_start(spot_px=spot_px, ts=ts)
        await self._apply(actions, ts=ts)
        self._started = True
        log.info(
            "continuous_driver.start",
            strategy_id=self.strategy.strategy_id,
            instrument_id=self.strategy.instrument_id,
            n_orders=len(self.book),
        )

    async def on_tick(self, *, px: float, ts: float) -> None:
        self.stats.ticks += 1
        self.stats.last_px = px
        self.stats.last_ts = ts

        if SimulatedExecutionClient.kill_switch_active():
            actions = self.strategy.on_kill_switch()
            await self._apply(actions, ts=ts)
            return

        # 1. Strategy-side reaction (may place/cancel/reset based on px).
        pre_actions = self.strategy.on_trade_tick(px=px, ts=ts)
        await self._apply(pre_actions, ts=ts)

        # 2. Book fills against the current tick.
        fills = await self.book.on_tick(instrument_id=self.strategy.instrument_id, px=px, ts=ts)
        self.stats.fills += len(fills)

        # 3. Strategy reacts to fills (re-place the opposite-side level, etc.).
        for f in fills:
            post = self.strategy.on_fill(f)
            await self._apply(post, ts=ts)

    async def on_bar_1m(self, bar: Bar) -> None:
        actions = self.strategy.on_bar_1m(bar)
        await self._apply(actions, ts=bar.ts_close)

    async def stop(self, *, ts: float) -> None:
        actions = self.strategy.on_stop()
        await self._apply(actions, ts=ts)
        log.info(
            "continuous_driver.stop",
            strategy_id=self.strategy.strategy_id,
            stats=self.stats.__dict__,
        )

    async def run_with_iterator(
        self,
        ticks: AsyncIterator[tuple[str, float, float]],
        *,
        start_spot_px: float,
        start_ts: float,
    ) -> None:
        """Drive the strategy from an async tick iterator. Useful for
        backtests and integration tests without Redis."""
        await self.start(spot_px=start_spot_px, ts=start_ts)
        try:
            async for instrument_id, px, ts in ticks:
                if instrument_id != self.strategy.instrument_id:
                    continue
                await self.on_tick(px=px, ts=ts)
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop(ts=self.stats.last_ts or start_ts)

    async def _apply(self, actions: list[Action], *, ts: float) -> None:
        for a in actions:
            try:
                await self._apply_one(a, ts=ts)
            except Exception as e:
                self.stats.errors += 1
                log.error(
                    "continuous_driver.apply_err",
                    strategy_id=self.strategy.strategy_id,
                    action_type=type(a).__name__,
                    err=str(e),
                )

    async def _apply_one(self, action: Action, *, ts: float) -> None:
        if isinstance(action, Place):
            ok = await self.book.place(action.order)
            if ok:
                self.stats.placed += 1
        elif isinstance(action, Cancel):
            ok = await self.book.cancel(action.coid, reason=action.reason)
            if ok:
                self.stats.cancelled += 1
        elif isinstance(action, CancelAll):
            n = await self.book.cancel_all(
                strategy_id=self.strategy.strategy_id,
                reason=action.reason,
            )
            self.stats.cancelled += n
        elif isinstance(action, Reset):
            # Atomic: snapshot realised pnl in strategy *before* cancelling,
            # then cancel only BUYs when the strategy runs buy-only mode
            # (3.8a.2 fix). Paired SELLs opened by prior-generation BUY
            # fills are the closing orders for already-accumulated long
            # inventory; discarding them would strand the inventory.
            # In symmetric mode the whole grid is cancelled as before.
            buy_only = bool(getattr(self.strategy, "buy_only", False))
            side_to_cancel = "BUY" if buy_only else None
            n_cancel = await self.book.cancel_all(
                strategy_id=self.strategy.strategy_id,
                side=side_to_cancel,
                reason=f"reset:{action.reason}",
            )
            self.stats.cancelled += n_cancel
            self.stats.resets += 1
            # Strategy's ``on_reset_applied`` bookkeeping runs after the
            # physical cancel — used to log metrics, not to emit more
            # actions. Placement of the new grid is expected to be driven
            # from the strategy's next ``on_trade_tick`` (or on_bar_1m).
            self.strategy.on_reset_applied(
                old_center=getattr(self.strategy, "center_price", 0.0),
                new_center=action.new_center,
                realized_pnl=getattr(self.strategy, "realized_pnl", 0.0),
                ts=ts,
            )
        else:
            raise TypeError(f"unknown action type: {type(action).__name__}")
