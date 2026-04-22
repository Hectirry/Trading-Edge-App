"""TradingNode factory.

Phase 2 wires only the backtest mode. Paper and live raise NotImplementedError
pointing at Phase 3 and Phase 6. See ADR 0006.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Protocol


class TradingNode(Protocol):
    mode: str

    def start(self) -> None: ...
    def stop(self) -> None: ...


class BacktestNode:
    """Stub node handle for backtest mode. The real work happens inside
    src/trading/engine/backtest_driver.py; this class exists so callers
    can obtain a uniform handle across modes."""

    mode = "backtest"

    def __init__(self, strategy_name: str) -> None:
        self.strategy_name = strategy_name

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class PaperNode:
    """Paper mode handle. Real engine lives in trading.cli.paper_engine;
    this class is the factory contract (Invariant I.1 — same interface
    across modes)."""

    mode = "paper"

    def __init__(self, strategy_name: str) -> None:
        self.strategy_name = strategy_name

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


KILL_SWITCH_PATH = "/etc/trading-system/KILL_SWITCH"


def kill_switch_active() -> bool:
    return Path(KILL_SWITCH_PATH).exists()


def check_live_file() -> bool:
    return Path("/etc/trading-system/I_UNDERSTAND_THIS_IS_REAL_MONEY").exists()


def create_trading_node(
    mode: Literal["backtest", "paper", "live"],
    strategy_name: str,
) -> TradingNode:
    if mode == "backtest":
        # Kill switch is ignored in backtest (see ADR 0005).
        if kill_switch_active():
            # Log-only; do not refuse.
            pass
        return BacktestNode(strategy_name)

    if kill_switch_active():
        raise RuntimeError(
            "KILL_SWITCH active — refusing to start paper/live node. "
            f"Remove {KILL_SWITCH_PATH} to re-enable."
        )

    if mode == "paper":
        return PaperNode(strategy_name)

    if mode == "live":
        if not check_live_file():
            raise RuntimeError(
                "live mode requires /etc/trading-system/I_UNDERSTAND_THIS_IS_REAL_MONEY"
            )
        if os.environ.get("TRADING_ENV") != "production":
            raise RuntimeError("live mode requires TRADING_ENV=production explicitly set.")
        raise NotImplementedError(
            "live mode wiring ships in Phase 6. Use --mode=backtest for Phase 2."
        )

    raise ValueError(f"unknown mode: {mode}")
