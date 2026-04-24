"""Export the most recent completed row of research.backtests (optionally
filtered by --strategy) into a markdown summary under
estrategias/resultados/<name>/backtest-YYYY-MM-DD.md.

Invoked by scripts/vps_daily.sh after each backtest. Token-efficient:
one file per run, headline metrics + verdict + pointers to HTML + DB.

Usage:
  python scripts/export_result_md.py --strategy polymarket_btc5m/trend_confirm_t1_v1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

OUT_ROOT = Path(__file__).resolve().parent.parent / "estrategias" / "resultados"

VERDICT_RULES = {
    "OK": lambda m: (
        m["n_trades"] >= 30
        and m["sharpe_per_trade"] >= 0.10
        and m["total_pnl"] > 0.0
        and m["mdd_usd"] > -20.0
    ),
    "FAIL": lambda m: m["total_pnl"] < 0.0 or m["sharpe_per_trade"] < 0.0,
}


def verdict(m: dict) -> str:
    if VERDICT_RULES["FAIL"](m):
        return "FAIL"
    if VERDICT_RULES["OK"](m):
        return "OK"
    return "MARGINAL"


async def fetch_latest(strategy: str | None) -> dict:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        q = (
            "SELECT id, strategy_name, strategy_commit, params_hash, "
            "dataset_from, dataset_to, started_at, data_source, "
            "report_path, metrics "
            "FROM research.backtests WHERE status = 'completed' "
        )
        args: list = []
        if strategy:
            q += "AND strategy_name = $1 "
            args.append(strategy)
        q += "ORDER BY started_at DESC LIMIT 1"
        row = await conn.fetchrow(q, *args)
        if row is None:
            raise SystemExit(f"no completed backtest found for strategy={strategy}")
        return dict(row)
    finally:
        await conn.close()


def render(row: dict) -> tuple[str, str, str]:
    import json

    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    perf = metrics.get("performance", {})
    risk = metrics.get("risk_adjusted", {})
    m = {
        "n_trades": int(perf.get("n_trades", 0)),
        "win_rate": float(perf.get("win_rate", 0.0)),
        "total_pnl": float(perf.get("total_pnl", 0.0)),
        "sharpe_per_trade": float(risk.get("sharpe_per_trade", 0.0)),
        "sharpe_daily": float(risk.get("sharpe_daily", 0.0)),
        "mdd_usd": float(risk.get("mdd_usd", 0.0)),
    }
    v = verdict(m)
    strat_name = row["strategy_name"]
    short_name = strat_name.split("/")[-1]
    started = row["started_at"].astimezone(UTC).replace(microsecond=0)
    ymd = started.strftime("%Y-%m-%d")
    started_iso = started.isoformat()
    commit_short = (row["strategy_commit"] or "")[:8]
    body = f"""# backtest — {strat_name}

Fecha corrida: {started_iso}
Commit: {commit_short}
Params hash: {row["params_hash"]}
Ventana datos: {row["dataset_from"].isoformat()} → {row["dataset_to"].isoformat()}
Fuente: {row["data_source"]}
Backtest ID: {row["id"]}
Reporte HTML: {row["report_path"] or "(no generado)"}

## Verdict

**{v}**

## Métricas

| métrica | valor |
|---|---|
| n_trades | {m["n_trades"]} |
| win_rate | {m["win_rate"] * 100:.1f}% |
| total_pnl | ${m["total_pnl"]:.2f} |
| sharpe / trade | {m["sharpe_per_trade"]:.3f} |
| sharpe diario | {m["sharpe_daily"]:.3f} |
| mdd (USD) | ${m["mdd_usd"]:.2f} |

## Notas

_(vacío — Claude edita este bloque al leer el resultado si hay algo no-obvio)_

## Drill-down

- HTML: `{row["report_path"] or "(no generado)"}`
- DB: `SELECT * FROM research.backtest_trades WHERE backtest_id = '{row["id"]}';`
- Grafana: `/grafana` → backtests → `started_at = {started_iso}`
"""
    return short_name, ymd, body


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", help="strategy_name filter (e.g. polymarket_btc5m/foo_v1)")
    args = ap.parse_args()
    row = await fetch_latest(args.strategy)
    short_name, ymd, body = render(row)
    out_dir = OUT_ROOT / short_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"backtest-{ymd}.md"
    # If same-day rerun, suffix with HHMM to avoid overwrite.
    if out_path.exists():
        hhmm = datetime.now(UTC).strftime("%H%M")
        out_path = out_dir / f"backtest-{ymd}-{hhmm}.md"
    out_path.write_text(body)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
