"""Compare Trading-Edge-App backtest trades against a polybot JSON trade vector.

Usage: python scripts/diff_parity.py <polybot.json> <tea_csv>
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def load_polybot(p: str) -> list[dict]:
    d = json.loads(Path(p).read_text())
    return [
        {
            "slug": t["market_slug"],
            "side": t["side"],
            "entry_ts": round(t["entry_ts"], 3),
            "entry_price": round(t["entry_price"], 6),
            "exit_price": t["exit_price"],
            "pnl_usd": round(t["pnl_usd"], 4),
        }
        for t in d["trades"]
    ]


def load_tea(p: str) -> list[dict]:
    out: list[dict] = []
    with open(p) as f:
        for r in csv.DictReader(f):
            out.append(
                {
                    "slug": r["market_slug"],
                    "side": r["side"],
                    "entry_ts": round(float(r["entry_ts"]), 3),
                    "entry_price": round(float(r["entry_price"]), 6),
                    "exit_price": float(r["exit_price"]),
                    "pnl_usd": round(float(r["pnl_usd"]), 4),
                }
            )
    return out


def main():
    if len(sys.argv) != 3:
        print("usage: diff_parity.py <polybot.json> <tea.csv>")
        sys.exit(1)
    p = load_polybot(sys.argv[1])
    t = load_tea(sys.argv[2])
    p_by = {(x["slug"], x["side"]): x for x in p}
    t_by = {(x["slug"], x["side"]): x for x in t}
    only_p = set(p_by) - set(t_by)
    only_t = set(t_by) - set(p_by)
    common = set(p_by) & set(t_by)
    print(f"polybot: {len(p)} trades")
    print(f"tea:     {len(t)} trades")
    print(f"common:  {len(common)}")
    print(f"only polybot: {len(only_p)}")
    print(f"only tea:     {len(only_t)}")
    if only_p:
        print("\nOnly in polybot (first 20):")
        for k in sorted(only_p)[:20]:
            row = p_by[k]
            print(
                f"  {row['slug']} {row['side']} entry_ts={row['entry_ts']} "
                f"price={row['entry_price']} pnl={row['pnl_usd']}"
            )
    if only_t:
        print("\nOnly in tea (first 20):")
        for k in sorted(only_t)[:20]:
            row = t_by[k]
            print(
                f"  {row['slug']} {row['side']} entry_ts={row['entry_ts']} "
                f"price={row['entry_price']} pnl={row['pnl_usd']}"
            )
    diff_price = 0
    diff_pnl = 0
    for k in common:
        if abs(p_by[k]["entry_price"] - t_by[k]["entry_price"]) > 1e-6:
            diff_price += 1
        if abs(p_by[k]["pnl_usd"] - t_by[k]["pnl_usd"]) > 0.01:
            diff_pnl += 1
    print(f"\nCommon trades with different entry_price: {diff_price}")
    print(f"Common trades with different pnl_usd: {diff_pnl}")


if __name__ == "__main__":
    main()
