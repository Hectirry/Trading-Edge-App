"""Strategy abstract base. Same surface as future Nautilus `Strategy`
subclass to keep the Phase 3 migration cheap (see ADR 0006)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from trading.engine.types import Decision, TickContext


class StrategyBase(ABC):
    name: str = "base"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.params: dict = config.get("params", config)

    def on_start(self) -> None:  # noqa: B027  -- optional override
        return

    def on_stop(self) -> None:  # noqa: B027  -- optional override
        return

    @abstractmethod
    def should_enter(self, ctx: TickContext) -> Decision: ...

    def on_trade_resolved(  # noqa: B027  -- optional override
        self, resolution: str, pnl: float, ts: float
    ) -> None:
        return
