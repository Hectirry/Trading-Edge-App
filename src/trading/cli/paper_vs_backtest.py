"""Weekly paper-vs-backtest comparison.

Reads:
  - trading.fills mode='paper' over the target week as the ground-truth
    paper trades.
  - market_data.paper_ticks over the same week and replays the backtest
    driver against them.

Persists a row to research.paper_vs_backtest_comparisons and posts a
summary to Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tomli

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger
from trading.engine.backtest_driver import (
    EntryWindowConfig,
    FillConfig,
    IndicatorConfig,
    run_backtest,
)
from trading.engine.risk import RiskManager
from trading.notifications import telegram as T
from trading.paper.backtest_loader import PaperTicksLoader
from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3

log = get_logger("cli.paper_vs_backtest")


@dataclass
class PaperTrade:
    market_slug: str
    side: str
    entry_ts: float
    entry_price: float
    pnl: float


async def _load_paper_trades(from_ts: float, to_ts: float) -> list[PaperTrade]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.instrument_id, o.side, o.ts_submit, f_entry.price AS entry_price,
                   (f_settle.metadata::jsonb->>'pnl')::numeric AS pnl,
                   split_part(o.instrument_id, '-YES.POLYMARKET', 1) AS base
            FROM trading.orders o
            JOIN trading.fills f_entry
                ON f_entry.order_id = o.order_id
                AND f_entry.metadata::jsonb->>'kind'='entry'
            LEFT JOIN trading.fills f_settle
                ON f_settle.order_id = o.order_id
                AND f_settle.metadata::jsonb->>'kind'='settle'
            WHERE o.mode='paper'
              AND o.ts_submit >= to_timestamp($1)
              AND o.ts_submit <  to_timestamp($2)
            ORDER BY o.ts_submit
            """,
            from_ts,
            to_ts,
        )
    out: list[PaperTrade] = []
    for r in rows:
        slug = r["instrument_id"].rsplit("-", 1)[
            0
        ]  # strip trailing -YES.POLYMARKET or -NO.POLYMARKET
        if slug.endswith(".POLYMARKET"):
            slug = slug.rsplit(".POLYMARKET", 1)[0]
        if slug.endswith("-YES") or slug.endswith("-NO"):
            slug = slug.rsplit("-", 1)[0]
        out.append(
            PaperTrade(
                market_slug=slug,
                side="YES_UP",  # strategy imbalance_v3 is long-only YES_UP (ADR note)
                entry_ts=r["ts_submit"].timestamp(),
                entry_price=float(r["entry_price"]),
                pnl=float(r["pnl"] or 0.0),
            )
        )
    return out


async def _run(args) -> None:
    # Resolve week window.
    now = datetime.now(tz=UTC)
    if args.week == "last":
        week_end = datetime(now.year, now.month, now.day, tzinfo=UTC)
        week_end -= timedelta(days=week_end.weekday() + 1)  # prev Sunday
        week_start = week_end - timedelta(days=7)
    else:
        week_start = datetime.fromisoformat(args.week).replace(tzinfo=UTC)
        week_end = week_start + timedelta(days=7)

    log.info(
        "paper_vs_backtest.window",
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
    )

    strategy_cfg = tomli.loads(Path(args.params).read_text())
    strategy = ImbalanceV3(config=strategy_cfg)
    loader = PaperTicksLoader(dsn=get_settings().pg_dsn)

    bt = run_backtest(
        strategy=strategy,
        loader=loader,
        from_ts=week_start.timestamp(),
        to_ts=week_end.timestamp(),
        stake_usd=min(
            float(strategy_cfg["sizing"]["stake_usd"]),
            float(strategy_cfg["risk"]["max_position_size_usd"]),
        ),
        fill_cfg=FillConfig(
            slippage_bps=float(strategy_cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(strategy_cfg["fill_model"]["fill_probability"]),
        ),
        entry_window=EntryWindowConfig(
            earliest_entry_t_s=int(strategy_cfg["backtest"]["earliest_entry_t_s"]),
            latest_entry_t_s=int(strategy_cfg["backtest"]["latest_entry_t_s"]),
        ),
        risk_manager=RiskManager({"risk": strategy_cfg["risk"]}),
        config_used=strategy_cfg,
        indicator_cfg=IndicatorConfig(),
        seed=42,
    )

    paper = await _load_paper_trades(week_start.timestamp(), week_end.timestamp())
    paper_set = {(p.market_slug, p.side): p for p in paper}
    bt_set = {(t.market_slug, t.side): t for t in bt.trades}
    common = set(paper_set) & set(bt_set)

    paper_n = len(paper)
    bt_n = bt.n_trades
    paper_pnl = sum(p.pnl for p in paper)
    bt_pnl = sum(t.pnl_usd for t in bt.trades)

    def _pct(a: float, b: float) -> float:
        if b == 0:
            return 0.0 if a == 0 else 1.0
        return (a - b) / abs(b)

    delta_trades_pct = _pct(float(paper_n), float(bt_n))
    delta_pnl_pct = _pct(paper_pnl, bt_pnl)

    verdict = "aligned"
    if abs(delta_trades_pct) > 0.10 or abs(delta_pnl_pct) > 0.20:
        verdict = "divergent"

    detail = {
        "paper_n": paper_n,
        "backtest_n": bt_n,
        "common": len(common),
        "only_paper": sorted([k[0] for k in set(paper_set) - set(bt_set)])[:20],
        "only_backtest": sorted([k[0] for k in set(bt_set) - set(paper_set)])[:20],
        "paper_pnl": paper_pnl,
        "backtest_pnl": bt_pnl,
    }

    run_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research.paper_vs_backtest_comparisons
                (id, strategy_name, week_start, week_end,
                 paper_trades, backtest_trades, paper_pnl, backtest_pnl,
                 delta_trades_pct, delta_pnl_pct, common_trades, verdict, detail)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
            """,
            run_id,
            strategy.name,
            week_start,
            week_end,
            paper_n,
            bt_n,
            paper_pnl,
            bt_pnl,
            delta_trades_pct,
            delta_pnl_pct,
            len(common),
            verdict,
            json.dumps(detail),
        )

    msg = (
        f"📉 Paper-vs-backtest — week of {week_start.date()}\n"
        f"Paper:    n={paper_n}  pnl=${paper_pnl:+.2f}\n"
        f"Backtest: n={bt_n}  pnl=${bt_pnl:+.2f}\n"
        f"Δ trades: {delta_trades_pct*100:+.1f}%   Δ pnl: {delta_pnl_pct*100:+.1f}%\n"
        f"Common slugs: {len(common)}\n"
        f"Verdict: {verdict.upper()}"
    )
    log.info("paper_vs_backtest.done", verdict=verdict, paper_n=paper_n, bt_n=bt_n)

    if args.print_only:
        print(msg)
        return

    tg = T.TelegramClient()
    await tg.send(T.AlertEvent(kind=f"WEEKLY_COMPARISON_{week_start.isoformat()}", text=msg))
    await tg.aclose()


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.paper_vs_backtest")
    p.add_argument("--week", default="last", help="'last' or YYYY-MM-DD (week start)")
    p.add_argument(
        "--params",
        default="config/strategies/pbt5m_imbalance_v3.toml",
    )
    p.add_argument("--print-only", action="store_true")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
