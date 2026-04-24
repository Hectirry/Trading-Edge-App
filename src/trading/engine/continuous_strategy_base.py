"""Continuous strategy abstract base (ADR 3.8a).

Grid trading maintains N persistent limit orders and reacts to tick /
bar streams — it does not fit the decision-at-tick ``StrategyBase``
shape (``should_enter → Decision``). ``ContinuousStrategyBase`` gives
grid-style strategies a lifecycle centred on order-book state rather
than per-tick decisions.

Lifecycle hooks (all optional except ``on_start``):

    on_start(engine)                 — place initial orders
    on_trade_tick(px, ts)            — tick arrival (pre-fill check);
                                       return list of actions the driver
                                       should execute
    on_bar_1m(bar)                   — 1m OHLCV refresh (ATR, HMM, etc.)
    on_fill(fill)                    — fill notification from driver
    on_reset_triggered(new_center)   — driver calls this when the
                                       strategy returns ``Reset(...)``
    on_kill_switch()                 — flatten + cancel-all
    on_stop()                        — shutdown

Actions returned by ``on_trade_tick`` are plain dataclasses (Place /
Cancel / CancelAll / Reset). The driver applies them against the
``LimitBookSim`` in order, which keeps strategy code pure wrt I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from trading.paper.limit_book_sim import LimitFill, LimitOrder


@dataclass
class Bar:
    ts_open: float
    ts_close: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Place:
    order: LimitOrder


@dataclass
class Cancel:
    coid: str
    reason: str = "user"


@dataclass
class CancelAll:
    reason: str = "reset"


@dataclass
class Reset:
    new_center: float
    reason: str = "boundary_breach"
    metadata: dict[str, Any] = field(default_factory=dict)


Action = Place | Cancel | CancelAll | Reset


class ContinuousStrategyBase(ABC):
    name: str = "base-continuous"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.params: dict = config.get("params", config)
        self.strategy_id: str = config.get("strategy_id", self.name)
        self.instrument_id: str = config.get("instrument_id", "BTCUSDT.BINANCE")

    @abstractmethod
    def on_start(self, *, spot_px: float, ts: float) -> list[Action]:
        """Return initial actions (usually a batch of Place)."""
        ...

    def on_trade_tick(  # noqa: B027
        self, *, px: float, ts: float
    ) -> list[Action]:
        return []

    def on_bar_1m(self, bar: Bar) -> list[Action]:  # noqa: B027
        return []

    def on_fill(self, fill: LimitFill) -> list[Action]:  # noqa: B027
        return []

    def on_reset_applied(  # noqa: B027
        self, *, old_center: float, new_center: float, realized_pnl: float, ts: float
    ) -> None:
        return

    def on_kill_switch(self) -> list[Action]:  # noqa: B027
        return [CancelAll(reason="kill_switch")]

    def on_stop(self) -> list[Action]:  # noqa: B027
        return [CancelAll(reason="stop")]
