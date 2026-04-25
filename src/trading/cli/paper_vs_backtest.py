"""Weekly paper-vs-backtest reconciliation.

For each tracked strategy, aggregate paper-mode trades from
``trading.orders`` + ``trading.fills`` (settle kind) over a window,
look up the matching ``research.backtests`` row covering that window,
and emit one row into ``research.paper_vs_backtest_comparisons``.

Verdict:
  - OK     : |delta_pnl_pct| <= drift_pct_warn AND |delta_trades_pct| <= drift_pct_warn
  - DRIFT  : either metric outside warn band but within fail band
  - FAIL   : either metric outside fail band, or paper has zero trades
             when backtest has many (signals broken paper engine)

Usage:
  python -m trading.cli.paper_vs_backtest --week-start 2026-04-18T00:00:00Z
  python -m trading.cli.paper_vs_backtest --rolling 7d
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger

log = get_logger("cli.paper_vs_backtest")

STRATEGIES = (
    "trend_confirm_t1_v1",
    "last_90s_forecaster_v1",
    "last_90s_forecaster_v2",
    "contest_ensemble_v1",
    "contest_avengers_v1",
)
DRIFT_WARN_PCT = 30.0
DRIFT_FAIL_PCT = 60.0


@dataclass
class PaperAgg:
    n_trades: int
    n_wins: int
    total_pnl: float
    instruments: set[str]


@dataclass
class BacktestAgg:
    backtest_id: str | None
    n_trades: int
    n_wins: int
    total_pnl: float
    dataset_from: datetime | None
    dataset_to: datetime | None


async def _aggregate_paper(conn, strategy_id: str, since: datetime, until: datetime) -> PaperAgg:
    rows = await conn.fetch(
        """
        SELECT o.instrument_id,
               (f.metadata::jsonb->>'resolution') AS resolution,
               COALESCE((f.metadata::jsonb->>'pnl')::numeric, 0) AS pnl
        FROM trading.orders o
        JOIN trading.fills f
          ON f.order_id = o.order_id
         AND f.metadata::jsonb->>'kind' = 'settle'
        WHERE o.mode = 'paper'
          AND o.strategy_id = $1
          AND o.ts_submit >= $2 AND o.ts_submit < $3
        """,
        strategy_id,
        since,
        until,
    )
    n_trades = len(rows)
    n_wins = sum(1 for r in rows if r["resolution"] == "win")
    total_pnl = float(sum(float(r["pnl"]) for r in rows))
    instruments = {r["instrument_id"] for r in rows if r["instrument_id"]}
    return PaperAgg(n_trades, n_wins, total_pnl, instruments)


async def _aggregate_backtest(
    conn, strategy_full_name: str, since: datetime, until: datetime
) -> BacktestAgg:
    """Pick the most-recent completed backtest whose dataset window
    overlaps [since, until). For now we just scan that single backtest's
    metrics — multi-backtest stitching is overkill until we have one."""
    row = await conn.fetchrow(
        """
        SELECT id, dataset_from, dataset_to, metrics
        FROM research.backtests
        WHERE status = 'completed'
          AND strategy_name = $1
          AND dataset_from < $3 AND dataset_to > $2
        ORDER BY started_at DESC
        LIMIT 1
        """,
        strategy_full_name,
        since,
        until,
    )
    if row is None:
        return BacktestAgg(None, 0, 0, 0.0, None, None)
    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    perf = metrics.get("performance", {}) if metrics else {}
    return BacktestAgg(
        backtest_id=str(row["id"]),
        n_trades=int(perf.get("n_trades", 0)),
        n_wins=int(perf.get("wins", 0)),
        total_pnl=float(perf.get("total_pnl", 0.0)),
        dataset_from=row["dataset_from"],
        dataset_to=row["dataset_to"],
    )


def _verdict(paper: PaperAgg, bt: BacktestAgg) -> tuple[str, float | None, float | None]:
    """Return (verdict, delta_trades_pct, delta_pnl_pct)."""
    if bt.n_trades == 0:
        return ("NO_BACKTEST", None, None)
    if paper.n_trades == 0:
        return ("FAIL", -100.0, -100.0)
    delta_trades_pct = (paper.n_trades - bt.n_trades) / bt.n_trades * 100.0
    if abs(bt.total_pnl) < 1e-9:
        delta_pnl_pct = float("nan")
    else:
        delta_pnl_pct = (paper.total_pnl - bt.total_pnl) / abs(bt.total_pnl) * 100.0
    worst = max(abs(delta_trades_pct), abs(delta_pnl_pct) if delta_pnl_pct == delta_pnl_pct else 0)
    if worst <= DRIFT_WARN_PCT:
        v = "OK"
    elif worst <= DRIFT_FAIL_PCT:
        v = "DRIFT"
    else:
        v = "FAIL"
    return (v, delta_trades_pct, delta_pnl_pct)


async def _persist(
    conn,
    strategy_full_name: str,
    week_start: datetime,
    week_end: datetime,
    paper: PaperAgg,
    bt: BacktestAgg,
    verdict: str,
    delta_trades_pct: float | None,
    delta_pnl_pct: float | None,
) -> None:
    detail = {
        "paper_wins": paper.n_wins,
        "paper_win_rate": (paper.n_wins / paper.n_trades) if paper.n_trades else None,
        "backtest_wins": bt.n_wins,
        "backtest_win_rate": (bt.n_wins / bt.n_trades) if bt.n_trades else None,
        "backtest_id": bt.backtest_id,
        "backtest_dataset_from": bt.dataset_from.isoformat() if bt.dataset_from else None,
        "backtest_dataset_to": bt.dataset_to.isoformat() if bt.dataset_to else None,
        "drift_warn_pct": DRIFT_WARN_PCT,
        "drift_fail_pct": DRIFT_FAIL_PCT,
    }
    common_trades = 0  # exact trade-level matching not implemented yet
    await conn.execute(
        """
        INSERT INTO research.paper_vs_backtest_comparisons
          (id, strategy_name, week_start, week_end,
           paper_trades, backtest_trades, paper_pnl, backtest_pnl,
           delta_trades_pct, delta_pnl_pct, common_trades, verdict, detail)
        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
        """,
        strategy_full_name,
        week_start,
        week_end,
        paper.n_trades,
        bt.n_trades,
        paper.total_pnl,
        bt.total_pnl,
        delta_trades_pct,
        delta_pnl_pct,
        common_trades,
        verdict,
        json.dumps(detail),
    )


async def _run(args: argparse.Namespace) -> int:
    if args.rolling:
        until = datetime.now(tz=UTC).replace(microsecond=0)
        since = until - _parse_rolling(args.rolling)
    else:
        since = _parse_iso(args.week_start)
        until = since + timedelta(days=7)
    log.info("paper_vs_backtest.window", since=since.isoformat(), until=until.isoformat())

    rows_out = []
    async with acquire() as conn:
        for short in STRATEGIES:
            full = f"polymarket_btc5m/{short}"
            paper = await _aggregate_paper(conn, short, since, until)
            bt = await _aggregate_backtest(conn, full, since, until)
            verdict, dt_pct, dp_pct = _verdict(paper, bt)
            await _persist(conn, full, since, until, paper, bt, verdict, dt_pct, dp_pct)
            rows_out.append((short, paper, bt, verdict, dt_pct, dp_pct))

    _print_table(since, until, rows_out)
    return 0


def _print_table(since: datetime, until: datetime, rows: list[tuple]) -> None:
    print(f"\nventana: {since.isoformat()} → {until.isoformat()}\n")
    hdr = (
        f"{'estrategia':<24} {'paper_n':>8} {'bt_n':>6} "
        f"{'paper_$':>10} {'bt_$':>10} {'Δn%':>7} {'Δ$%':>8} {'verdict':>10}"
    )
    print(hdr)
    print("-" * len(hdr))
    for short, p, bt, v, dn, dp in rows:
        dn_s = f"{dn:+.1f}" if dn is not None else "  -  "
        dp_s = f"{dp:+.1f}" if dp is not None and dp == dp else "  -  "
        print(
            f"{short:<24} {p.n_trades:>8} {bt.n_trades:>6} "
            f"{p.total_pnl:>+10.2f} {bt.total_pnl:>+10.2f} "
            f"{dn_s:>7} {dp_s:>8} {v:>10}"
        )


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _parse_rolling(s: str) -> timedelta:
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    raise SystemExit(f"unrecognized rolling spec: {s} (use Nd or Nh)")


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.paper_vs_backtest")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--week-start", help="ISO timestamp; window is [start, start+7d)")
    g.add_argument("--rolling", help="rolling window like 7d / 24h ending now")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
