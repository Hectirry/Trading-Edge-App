"""Strategy abstract base. Same surface as future Nautilus `Strategy`
subclass to keep the Phase 3 migration cheap (see ADR 0006).

Two interaction modes (additive — existing direction-style strategies
keep using `should_enter` unchanged):

  Direction-style (one-shot ENTER): override `should_enter(ctx) -> Decision`.
  MM-style    (continuous quoting): override `on_tick(ctx) -> list[MMAction]`.

The paper / backtest driver dispatches by checking which method the
subclass has overridden (the base provides no-op defaults for both).
"""

from __future__ import annotations

from abc import ABC

from trading.engine.mm_actions import MMAction
from trading.engine.types import Action, Decision, TickContext


class StrategyBase(ABC):
    name: str = "base"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.params: dict = config.get("params", config)

    @staticmethod
    def build_breakdown(**checks) -> dict:
        """Uniform `signal_breakdown` helper — shape: {check_name: value}."""
        return dict(checks)

    def on_start(self) -> None:  # noqa: B027  -- optional override
        return

    def on_stop(self) -> None:  # noqa: B027  -- optional override
        return

    # Direction-style API. Default no-op so MM-only strategies don't have to
    # implement it; they should override `on_tick` instead.
    def should_enter(self, ctx: TickContext) -> Decision:  # noqa: B027
        return Decision(action=Action.SKIP, reason="not_implemented")

    # MM-style API. Default returns no actions; direction-style strategies
    # never override this and the driver detects that and skips MM dispatch.
    def on_tick(self, ctx: TickContext) -> list[MMAction]:  # noqa: B027
        return []

    # Fill callback for MM strategies. The driver invokes this after a
    # resting limit posted via `on_tick` is filled; the strategy uses it
    # to update inventory and the k_estimator. Direction-style strategies
    # ignore this hook.
    def on_fill(  # noqa: B027  -- optional override
        self,
        *,
        market_slug: str,
        client_order_id: str,
        side: str,
        fill_price: float,
        fill_qty_shares: float,
        ts: float,
    ) -> None:
        return

    def on_trade_resolved(  # noqa: B027  -- optional override
        self, resolution: str, pnl: float, ts: float
    ) -> None:
        return
