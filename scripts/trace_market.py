"""Trace a single market run through the TEA backtest driver, show each tick's
recomputed indicator values at a specific target ts. Usage:

docker exec tea-engine python scripts/trace_market.py <slug> <target_ts>
"""

from __future__ import annotations

import sys

from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.indicators import IndicatorStack


def main():
    slug = sys.argv[1]
    target_ts = float(sys.argv[2])
    loader = PolybotSQLiteLoader("/polybot-btc5m-data/polybot.db")
    for cur_slug, ticks in loader.iter_markets(target_ts - 600, target_ts + 600):
        if cur_slug != slug:
            continue
        stack = IndicatorStack()
        print(f"market: {slug} tick_count: {len(ticks)}")
        for ctx in ticks:
            stack.update(ctx)
            if abs(ctx.ts - target_ts) < 1.5:
                print(
                    f"  ts={ctx.ts:.2f} t={ctx.t_in_window:.1f} "
                    f"spot={ctx.spot_price:.2f} open={ctx.open_price:.2f} "
                    f"impl={ctx.implied_prob_yes:.4f} "
                    f"model={ctx.model_prob_yes:.4f} edge={ctx.edge*10000:+.1f}bps "
                    f"z={ctx.z_score:.3f} vol_ewma={ctx.vol_ewma:.4f} "
                    f"imb={ctx.pm_imbalance:.3f} spr={ctx.pm_spread_bps:.0f}"
                )
        return


if __name__ == "__main__":
    main()
