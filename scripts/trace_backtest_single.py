"""Run full backtest driver for a single target market, dump all per-tick
decisions. Usage:

docker exec tea-engine python scripts/trace_backtest_single.py <target_slug>
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomli

from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.indicators import IndicatorStack
from trading.engine.risk import RiskManager
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action
from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3


def main():
    target_slug = sys.argv[1]
    cfg = tomli.loads(Path("config/strategies/pbt5m_imbalance_v3.toml").read_text())
    strategy: StrategyBase = ImbalanceV3(config=cfg)
    risk = RiskManager({"risk": cfg["risk"]})
    loader = PolybotSQLiteLoader("/polybot-btc5m-data/polybot.db")

    try:
        close_ts = float(target_slug.rsplit("-", 1)[-1])
    except ValueError:
        print("bad slug")
        return

    # Grab the whole from the time the market first appears through close.
    for slug, ticks in loader.iter_markets(close_ts - 86400, close_ts + 10):
        if slug != target_slug:
            continue
        stack = IndicatorStack()
        recent_ctxs = []
        entered = False
        for ctx in ticks:
            stack.update(ctx)
            ctx.recent_ticks = recent_ctxs[-30:]
            recent_ctxs.append(ctx)
            if len(recent_ctxs) > 60:
                recent_ctxs.pop(0)
            if entered:
                continue
            if not (120 <= ctx.t_in_window <= 240):
                continue
            allowed, reason = risk.can_enter(ctx)
            if not allowed:
                if 130 <= ctx.t_in_window <= 140:
                    print(f"t={ctx.t_in_window:.1f} RISK_SKIP: {reason}")
                continue
            decision = strategy.should_enter(ctx)
            if 130 <= ctx.t_in_window <= 140:
                print(
                    f"t={ctx.t_in_window:.1f} strat={decision.action.value} "
                    f"reason={decision.reason}"
                )
            if decision.action is Action.ENTER:
                print(f">>> ENTER at ts={ctx.ts} t={ctx.t_in_window:.1f}")
                entered = True
                # simulate trade close
                risk.on_trade_closed(pnl=-3.0, now=close_ts)
        return


if __name__ == "__main__":
    main()
