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


# Dual-path KILL_SWITCH (ADR 0009). Engine reads both; API writes the
# /var/tea/control path (it cannot write /etc/ read-only mount).
KILL_SWITCH_PATHS: tuple[str, ...] = (
    "/etc/trading-system/KILL_SWITCH",
    "/var/tea/control/KILL_SWITCH",
)
# Back-compat alias: some logs/messages still reference the primary path.
KILL_SWITCH_PATH = KILL_SWITCH_PATHS[0]


def kill_switch_active() -> bool:
    return any(Path(p).exists() for p in KILL_SWITCH_PATHS)


def kill_switch_which() -> str | None:
    for p in KILL_SWITCH_PATHS:
        if Path(p).exists():
            return p
    return None


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
        which = kill_switch_which()
        raise RuntimeError(
            "KILL_SWITCH active — refusing to start paper/live node. "
            f"Remove {which} to re-enable."
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
