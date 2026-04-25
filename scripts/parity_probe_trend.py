"""Parity probe for trend_confirm_t1_v1 vs BTC-Tendencia-5m trades table.

Usage:
  docker exec tea-engine python scripts/parity_probe_trend.py \\
    /btc-tendencia-data/polybot-agent.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import tomli

from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, run_backtest
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.risk import RiskManager
from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1


def main():
    if len(sys.argv) != 2:
        print("usage: parity_probe_trend.py <polybot-agent.db>")
        sys.exit(1)
    db_path = sys.argv[1]

    # Reference: trades from polybot-agent.db labelled as trend_confirm_t1_v1 paper.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    # Scope parity to the stable-config window only (2026-04-22, stake_usd=5).
    # Earlier trades (2026-04-19 → 2026-04-20) used stake_usd=1.5 under an
    # earlier config that is no longer faithfully reproducible.
    STABLE_FROM = 1776820000.0  # ~2026-04-22 08:00 UTC
    ref_rows = cur.execute(
        """
        SELECT market_slug, side, entry_ts, entry_price, pnl_usd
        FROM trades
        WHERE strategy='trend_confirm_t1_v1' AND mode_tag='paper'
          AND entry_ts >= ?
        ORDER BY entry_ts
        """,
        (STABLE_FROM,),
    ).fetchall()
    bounds = cur.execute(
        "SELECT min(ts), max(ts) FROM ticks WHERE ts >= ?", (STABLE_FROM,)
    ).fetchone()
    conn.close()

    if not ref_rows:
        print("no paper trades found for trend_confirm_t1_v1")
        sys.exit(1)
    from_ts = float(bounds[0])
    to_ts = float(bounds[1])
    print(f"period: {from_ts:.0f} -> {to_ts:.0f}  ref_trades: {len(ref_rows)}")

    ref_set = {(r[0], r[1]): {"entry_ts": r[2], "entry_price": r[3], "pnl": r[4]} for r in ref_rows}

    cfg = tomli.loads(Path("config/strategies/pbt5m_trend_confirm_t1_v1.toml").read_text())
    strategy = TrendConfirmT1V1(config=cfg)
    # BTC-Tendencia-5m slug = open_ts (not close_ts like polybot-btc5m).
    loader = PolybotSQLiteLoader(db_path, slug_encodes_open_ts=True)
    risk = RiskManager({"risk": cfg["risk"]})
    stake = min(
        float(cfg["sizing"]["stake_usd"]),
        float(cfg["risk"]["max_position_size_usd"]),
    )

    result = run_backtest(
        strategy=strategy,
        loader=loader,
        from_ts=from_ts,
        to_ts=to_ts,
        stake_usd=stake,
        fill_cfg=FillConfig(
            slippage_bps=float(cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(cfg["fill_model"]["fill_probability"]),
        ),
        entry_window=EntryWindowConfig(
            earliest_entry_t_s=int(cfg["backtest"]["earliest_entry_t_s"]),
            latest_entry_t_s=int(cfg["backtest"]["latest_entry_t_s"]),
        ),
        risk_manager=risk,
        config_used=cfg,
        seed=42,
    )
    tea_set = {(t.market_slug, t.side): t for t in result.trades}

    only_ref = set(ref_set) - set(tea_set)
    only_tea = set(tea_set) - set(ref_set)
    common = set(ref_set) & set(tea_set)

    print(f"tea trades: {result.n_trades}")
    print(f"common    : {len(common)}")
    print(f"only ref  : {len(only_ref)}")
    print(f"only tea  : {len(only_tea)}")

    if only_ref:
        print("\n-- first 10 only in ref --")
        for k in sorted(only_ref)[:10]:
            v = ref_set[k]
            print(
                f"  {k[0]} {k[1]} entry_ts={v['entry_ts']:.1f} "
                f"price={v['entry_price']:.4f} pnl={v['pnl']:+.2f}"
            )
    if only_tea:
        print("\n-- first 10 only in tea --")
        for k in sorted(only_tea)[:10]:
            t = tea_set[k]
            print(
                f"  {k[0]} {k[1]} entry_ts={t.entry_ts:.1f} "
                f"price={t.entry_price:.4f} pnl={t.pnl_usd:+.2f}"
            )

    price_drift = 0
    pnl_drift = 0
    for k in common:
        r = ref_set[k]
        t = tea_set[k]
        if abs(r["entry_price"] - t.entry_price) > 1e-4:
            price_drift += 1
        if abs(r["pnl"] - t.pnl_usd) > 0.05:
            pnl_drift += 1
    print(f"\ncommon with price drift >1e-4: {price_drift}")
    print(f"common with pnl drift >$0.05 : {pnl_drift}")


if __name__ == "__main__":
    main()
