"""Paso 0 — gate-firing analysis for trend_confirm_t1_v1 backtest.

Reads gate-firing booleans from research.backtest_trades.metadata
(populated by the instrumentation in commit f808b94) and produces:

    1. gate_correlation_matrix.csv — pairwise phi (Pearson on 0/1).
    2. gate_correlation_heatmap.png — visual of (1).
    3. gate_combinations_pnl.csv — per-combo signature: n, win_rate,
       avg_pnl, total_pnl.

Run:
    docker compose exec tea-engine python /app/scripts/analyze_gate_firings.py \\
        --backtest-id <uuid> \\
        --out-dir src/trading/research/reports
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

import asyncpg
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

GATES = ["f1", "f2", "f3", "f4", "f5", "f6", "f7"]
GATE_LABELS = {
    "f1": "fracdiff",
    "f2": "autocorr30",
    "f3": "cusum",
    "f4": "microprice",
    "f5": "mc_bootstrap",
    "f6": "candle60s",
    "f7": "prior_trend600s",
}
META_KEYS = {
    "f1": "f1_fracdiff_fired",
    "f2": "f2_autocorr_fired",
    "f3": "f3_cusum_fired",
    "f4": "f4_microprice_fired",
    "f5": "f5_mc_fired",
    "f6": "f6_candle_fired",
    "f7": "f7_prior_trend_fired",
}


async def fetch_trades(backtest_id: str, dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT
                trade_idx,
                pnl::float AS pnl,
                (pnl > 0)::int AS won,
                metadata
            FROM research.backtest_trades
            WHERE backtest_id = $1
            ORDER BY trade_idx
            """,
            backtest_id,
        )
    finally:
        await conn.close()
    out = []
    for r in rows:
        raw = r["metadata"]
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        feats = meta.get("signal_features") or {}
        gates = {g: bool(feats.get(META_KEYS[g], False)) for g in GATES}
        out.append(
            {
                "trade_idx": r["trade_idx"],
                "pnl": float(r["pnl"]),
                "won": int(r["won"]),
                "tau": int(feats.get("tau_seconds", -1)),
                "fav_ask": float(feats.get("fav_ask", 0.0)),
                **gates,
            }
        )
    return out


def correlation_matrix(trades: list[dict]) -> np.ndarray:
    arr = np.array([[int(t[g]) for g in GATES] for t in trades], dtype=float)
    n_features = arr.shape[1]
    corr = np.full((n_features, n_features), np.nan)
    for i in range(n_features):
        for j in range(n_features):
            xi, xj = arr[:, i], arr[:, j]
            if xi.std() == 0 or xj.std() == 0:
                corr[i, j] = np.nan
            else:
                corr[i, j] = float(np.corrcoef(xi, xj)[0, 1])
    return corr


def write_correlation_csv(corr: np.ndarray, out: Path) -> None:
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["", *[f"{g} ({GATE_LABELS[g]})" for g in GATES]])
        for i, g in enumerate(GATES):
            row = [f"{g} ({GATE_LABELS[g]})"]
            for j in range(len(GATES)):
                v = corr[i, j]
                row.append("nan" if np.isnan(v) else f"{v:.4f}")
            w.writerow(row)


def write_heatmap(corr: np.ndarray, out: Path, *, title: str) -> None:
    fig, ax = plt.subplots(figsize=(7, 6), dpi=130)
    cmap = plt.get_cmap("RdBu_r")
    im = ax.imshow(corr, vmin=-1, vmax=1, cmap=cmap)
    ax.set_xticks(range(len(GATES)))
    ax.set_yticks(range(len(GATES)))
    ax.set_xticklabels([f"{g}\n{GATE_LABELS[g]}" for g in GATES], fontsize=8)
    ax.set_yticklabels([f"{g} {GATE_LABELS[g]}" for g in GATES], fontsize=8)
    for i in range(len(GATES)):
        for j in range(len(GATES)):
            v = corr[i, j]
            txt = "—" if np.isnan(v) else f"{v:+.2f}"
            color = "white" if not np.isnan(v) and abs(v) > 0.55 else "black"
            ax.text(j, i, txt, ha="center", va="center", fontsize=7, color=color)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def write_combinations_csv(trades: list[dict], out: Path) -> None:
    by_combo: dict[str, list[dict]] = {}
    for t in trades:
        sig = "".join("1" if t[g] else "0" for g in GATES)
        by_combo.setdefault(sig, []).append(t)
    rows = []
    for sig, ts in by_combo.items():
        n = len(ts)
        wins = sum(t["won"] for t in ts)
        total_pnl = sum(t["pnl"] for t in ts)
        rows.append(
            {
                "combo_signature": sig,
                "fired_gates": ",".join(g for g, b in zip(GATES, sig) if b == "1"),
                "n_trades": n,
                "win_rate": wins / n if n else 0.0,
                "avg_pnl": total_pnl / n if n else 0.0,
                "total_pnl": total_pnl,
            }
        )
    rows.sort(key=lambda r: r["n_trades"], reverse=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "combo_signature",
                "fired_gates",
                "n_trades",
                "win_rate",
                "avg_pnl",
                "total_pnl",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "combo_signature": r["combo_signature"],
                    "fired_gates": r["fired_gates"],
                    "n_trades": r["n_trades"],
                    "win_rate": f"{r['win_rate']:.4f}",
                    "avg_pnl": f"{r['avg_pnl']:.4f}",
                    "total_pnl": f"{r['total_pnl']:.4f}",
                }
            )


def fire_rate_summary(trades: list[dict]) -> dict[str, float]:
    n = len(trades)
    return {g: sum(1 for t in trades if t[g]) / n for g in GATES}


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-id", required=True)
    parser.add_argument("--out-dir", default="src/trading/research/reports")
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "TEA_PG_DSN",
            "postgresql://tea@tea-postgres:5432/trading_edge",
        ),
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    trades = await fetch_trades(args.backtest_id, args.dsn)
    if not trades:
        raise SystemExit(f"no trades for backtest_id={args.backtest_id}")

    n = len(trades)
    fr = fire_rate_summary(trades)
    print(f"n_trades = {n}")
    print("fire-rate per gate:")
    for g in GATES:
        print(f"  {g} {GATE_LABELS[g]:<18} = {fr[g]*100:5.1f}%")

    corr = correlation_matrix(trades)
    csv_path = out_dir / "gate_correlation_matrix.csv"
    png_path = out_dir / "gate_correlation_heatmap.png"
    combos_path = out_dir / "gate_combinations_pnl.csv"

    write_correlation_csv(corr, csv_path)
    write_heatmap(
        corr,
        png_path,
        title=(
            f"trend_confirm_t1_v1 — gate firing correlation (N={n})\n"
            f"backtest_id={args.backtest_id[:8]}…"
        ),
    )
    write_combinations_csv(trades, combos_path)

    print()
    print("max abs off-diagonal pairs (top 5):")
    pairs = []
    for i in range(len(GATES)):
        for j in range(i + 1, len(GATES)):
            v = corr[i, j]
            if not np.isnan(v):
                pairs.append((abs(v), v, GATES[i], GATES[j]))
    pairs.sort(reverse=True)
    for absv, v, gi, gj in pairs[:5]:
        print(f"  corr({gi},{gj}) = {v:+.3f}  [{GATE_LABELS[gi]} vs {GATE_LABELS[gj]}]")
    print()
    print(f"wrote: {csv_path}")
    print(f"wrote: {png_path}")
    print(f"wrote: {combos_path}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
