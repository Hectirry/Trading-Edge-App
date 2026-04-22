"""Trend-confirm parity against polybot-agent *backtest* trade vector.

Ground truth: JSON file produced by /tmp/extract_polybot_agent_trades.py
(which imports polybot-agent's backtest_engine.run_single_backtest
read-only and dumps SimulatedTrade list). Both sides are deterministic
backtests, so parity must be 0 diffs.

Usage:
  docker exec tea-engine python scripts/parity_probe_trend_bt.py \
    /btc-tendencia-data/polybot-agent.db \
    /btc-tendencia-reports/polybot_agent_trades.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tomli

from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, run_backtest
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.risk import RiskManager
from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1


def main():
    if len(sys.argv) != 3:
        print("usage: parity_probe_trend_bt.py <polybot-agent.db> <polybot_agent_trades.json>")
        sys.exit(1)
    db_path, ref_path = sys.argv[1], sys.argv[2]

    ref = json.loads(Path(ref_path).read_text())
    if not ref:
        print("empty ref trades")
        sys.exit(1)
    ref_set = {
        (t["market_slug"], t["side"]): {
            "entry_ts": t["entry_ts"],
            "entry_price": t["entry_price"],
            "pnl": t["pnl_usd"],
            "fee": t.get("fee_usd", 0.0),
        }
        for t in ref
    }

    # Align range exactly with extract window used by
    # /tmp/extract_polybot_agent_trades.py (from_ts=1776816000, to_ts=1776888000).
    # Allow override via env for generalization.
    import os

    from_ts = float(os.environ.get("PARITY_FROM", "1776816000"))
    to_ts = float(os.environ.get("PARITY_TO", "1776888000"))
    print(f"ref trades: {len(ref)}  period: {from_ts:.0f} -> {to_ts:.0f}")

    cfg = tomli.loads(Path("config/strategies/pbt5m_trend_confirm_t1_v1.toml").read_text())
    strategy = TrendConfirmT1V1(config=cfg)
    loader = PolybotSQLiteLoader(db_path, slug_encodes_open_ts=True)

    result = run_backtest(
        strategy=strategy,
        loader=loader,
        from_ts=from_ts,
        to_ts=to_ts,
        stake_usd=min(
            float(cfg["sizing"]["stake_usd"]),
            float(cfg["risk"]["max_position_size_usd"]),
        ),
        fill_cfg=FillConfig(
            slippage_bps=float(cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(cfg["fill_model"]["fill_probability"]),
            apply_fee_in_backtest=bool(cfg["fill_model"].get("apply_fee_in_backtest", False)),
            fee_k=float(cfg["fill_model"].get("fee_k", 0.05)),
        ),
        entry_window=EntryWindowConfig(
            earliest_entry_t_s=int(cfg["backtest"]["earliest_entry_t_s"]),
            latest_entry_t_s=int(cfg["backtest"]["latest_entry_t_s"]),
        ),
        risk_manager=RiskManager({"risk": cfg["risk"]}),
        config_used=cfg,
        seed=42,
        bypass_risk=bool(cfg["risk"].get("bypass_in_backtest", False)),
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
        print("\n-- first 5 only in ref --")
        for k in sorted(only_ref)[:5]:
            v = ref_set[k]
            print(f"  {k[0]} {k[1]} entry_ts={v['entry_ts']:.1f} price={v['entry_price']:.4f} pnl={v['pnl']:+.2f}")
    if only_tea:
        print("\n-- first 5 only in tea --")
        for k in sorted(only_tea)[:5]:
            t = tea_set[k]
            print(f"  {k[0]} {k[1]} entry_ts={t.entry_ts:.1f} price={t.entry_price:.4f} pnl={t.pnl_usd:+.2f}")

    price_drift = 0
    pnl_drift = 0
    for k in common:
        r = ref_set[k]
        t = tea_set[k]
        if abs(r["entry_price"] - t.entry_price) > 1e-4:
            price_drift += 1
        if abs(r["pnl"] - t.pnl_usd) > 0.05:
            pnl_drift += 1
    print(f"\ncommon w/ price drift >1e-4 : {price_drift}")
    print(f"common w/ pnl drift >$0.05  : {pnl_drift}")


if __name__ == "__main__":
    main()
