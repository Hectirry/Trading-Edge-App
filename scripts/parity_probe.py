"""Run TEA imbalance_v3 and compare against polybot JSON. In-container usage:

docker exec tea-engine python scripts/parity_probe.py \
    /polybot-btc5m-data/polybot.db \
    /polybot-btc5m-reports/backtest_imbalance_v3_20260422_134025.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tomli

from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, run_backtest
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.risk import RiskManager
from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3


def main():
    if len(sys.argv) != 3:
        print("usage: parity_probe.py <polybot_db> <polybot_json>")
        sys.exit(1)
    db_path, json_path = sys.argv[1], sys.argv[2]

    cfg = tomli.loads(Path("config/strategies/pbt5m_imbalance_v3.toml").read_text())
    strategy = ImbalanceV3(config=cfg)

    ref = json.loads(Path(json_path).read_text())
    from_ts = float(ref["start_ts"])
    to_ts = float(ref["end_ts"])
    ref_trades = {
        (t["market_slug"], t["side"]): {
            "entry_ts": t["entry_ts"],
            "entry_price": t["entry_price"],
            "pnl": t["pnl_usd"],
        }
        for t in ref["trades"]
    }

    loader = PolybotSQLiteLoader(db_path)
    sizing = cfg.get("sizing", {})
    risk_cfg = cfg.get("risk", {})
    stake = min(float(sizing.get("stake_usd", 3.0)), float(risk_cfg.get("max_position_size_usd", 5.0)))

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
        risk_manager=RiskManager({"risk": risk_cfg}),
        config_used=cfg,
        seed=42,
    )
    tea_trades = {(t.market_slug, t.side): t for t in result.trades}
    print("tea decision counts:", result.decision_counts)

    only_ref = set(ref_trades) - set(tea_trades)
    only_tea = set(tea_trades) - set(ref_trades)
    common = set(ref_trades) & set(tea_trades)

    print(f"polybot ref trades: {len(ref_trades)}")
    print(f"tea trades        : {len(tea_trades)}")
    print(f"common            : {len(common)}")
    print(f"only in polybot   : {len(only_ref)}")
    print(f"only in tea       : {len(only_tea)}")

    if only_ref:
        print("\n-- trades only in polybot (first 10) --")
        for k in sorted(only_ref)[:10]:
            v = ref_trades[k]
            print(f"  {k[0]} {k[1]}  entry_ts={v['entry_ts']:.2f}  entry_price={v['entry_price']:.6f}  pnl={v['pnl']:+.2f}")
    if only_tea:
        print("\n-- trades only in tea (first 10) --")
        for k in sorted(only_tea)[:10]:
            t = tea_trades[k]
            print(f"  {k[0]} {k[1]}  entry_ts={t.entry_ts:.2f}  entry_price={t.entry_price:.6f}  pnl={t.pnl_usd:+.2f}")

    # Price/pnl drift on common.
    price_drift = 0
    pnl_drift = 0
    for k in common:
        r = ref_trades[k]
        t = tea_trades[k]
        if abs(r["entry_price"] - t.entry_price) > 1e-6:
            price_drift += 1
        if abs(r["pnl"] - t.pnl_usd) > 0.01:
            pnl_drift += 1
    print(f"\ncommon w/ price drift >1e-6 : {price_drift}")
    print(f"common w/ pnl drift >$0.01  : {pnl_drift}")


if __name__ == "__main__":
    main()
