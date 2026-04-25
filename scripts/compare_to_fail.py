"""Compare a fresh backtest of trend_confirm_t1_v1 (paper_ticks 23-Apr 12-18 UTC)
against the failing run 21dcdc91-…. Reports n_trades, win_rate, total_pnl,
sharpe_per_trade, mdd, and per-slug overlap (same side, changed resolution,
pnl drift). Argv: new_backtest_id.

Run: TEA_PG_HOST=localhost TEA_PG_PORT=5434 PYTHONPATH=src
     .venv/bin/python scripts/compare_to_fail.py <new_backtest_id>
"""

from __future__ import annotations

import asyncio
import sys

from trading.common.db import acquire, close_pool

FAIL_ID = "21dcdc91-994d-453c-a374-866c1168f4a7"


async def fetch_trades(conn, bid: str) -> dict[str, dict]:
    rows = await conn.fetch(
        """
        SELECT instrument, strategy_side, entry_price, exit_price, pnl, t_in_window_s
        FROM research.backtest_trades
        WHERE backtest_id = $1::uuid
        """,
        bid,
    )
    return {r["instrument"]: dict(r) for r in rows}


async def fetch_kpis(conn, bid: str) -> dict:
    row = await conn.fetchrow("SELECT metrics FROM research.backtests WHERE id = $1::uuid", bid)
    if row is None:
        return {}
    import json

    m = row["metrics"]
    if isinstance(m, str):
        m = json.loads(m)
    return m or {}


async def run(new_id: str) -> None:
    async with acquire() as conn:
        new_kpi = await fetch_kpis(conn, new_id)
        old_kpi = await fetch_kpis(conn, FAIL_ID)
        new_t = await fetch_trades(conn, new_id)
        old_t = await fetch_trades(conn, FAIL_ID)

    def _kpi_row(label, kpi):
        p = kpi.get("performance", {})
        ra = kpi.get("risk_adjusted", {})
        return (
            f"{label:>20} | n={p.get('n_trades', 0):>4} | "
            f"win_rate={p.get('win_rate', 0):.1%} | "
            f"pnl={p.get('total_pnl', 0):+.2f} | "
            f"sharpe/trade={ra.get('sharpe_per_trade', 0):+.3f} | "
            f"mdd={ra.get('mdd_usd', 0):+.2f}"
        )

    print(_kpi_row("FAIL (21dcdc91)", old_kpi))
    print(_kpi_row(f"new ({new_id[:8]})", new_kpi))
    print()

    # Per-slug comparison
    common = sorted(set(new_t) & set(old_t))
    only_new = sorted(set(new_t) - set(old_t))
    only_old = sorted(set(old_t) - set(new_t))
    same_side = 0
    res_changed = 0
    pnl_drift_total = 0.0
    side_changed = 0
    for slug in common:
        n = new_t[slug]
        o = old_t[slug]
        if n["strategy_side"] == o["strategy_side"]:
            same_side += 1
        else:
            side_changed += 1
        if (n["exit_price"] or 0) != (o["exit_price"] or 0):
            res_changed += 1
        pnl_drift_total += float(n["pnl"]) - float(o["pnl"])

    print(f"slugs in both runs: {len(common)}")
    print(f"slugs only in new : {len(only_new)}")
    print(f"slugs only in old : {len(only_old)}")
    print(f"same side (within common):     {same_side}/{len(common)}")
    print(f"side flipped:                  {side_changed}/{len(common)}")
    print(f"resolution changed (any side): {res_changed}/{len(common)}")
    print(f"sum(pnl_new − pnl_old) on common: {pnl_drift_total:+.2f}")

    # Win-rate validation band
    n_total = new_kpi.get("performance", {}).get("n_trades", 0)
    win_rate = new_kpi.get("performance", {}).get("win_rate", 0)
    print()
    print(f"new win_rate {win_rate:.1%} on {n_total} trades")
    if 0.55 <= win_rate <= 0.65:
        print("→ within expected band [55%, 65%] : FIX VALIDATED")
    else:
        print("→ outside expected band [55%, 65%] : NEEDS INVESTIGATION")

    await close_pool()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: compare_to_fail.py <new_backtest_id>")
        sys.exit(2)
    asyncio.run(run(sys.argv[1]))
