"""Backtest CLI.

Usage:
  python -m trading.cli.backtest \
    --strategy polymarket_btc5m/imbalance_v3 \
    --params config/strategies/pbt5m_imbalance_v3.toml \
    --from 2026-04-17T15:05:19Z --to 2026-04-21T23:59:59Z \
    --source polybot_sqlite \
    --polybot-db /polybot-btc5m-data/polybot.db
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import tomli

from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger
from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, run_backtest
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.node import create_trading_node
from trading.engine.risk import RiskManager
from trading.paper.backtest_loader import PaperTicksLoader
from trading.research.report import persist_and_render

log = get_logger("cli.backtest")


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _load_strategy(name: str, config: dict):
    if name == "polymarket_btc5m/imbalance_v3":
        from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3

        return ImbalanceV3(config=config)
    raise SystemExit(f"unknown strategy: {name}")


async def _run(args: argparse.Namespace) -> None:
    cfg = tomli.loads(Path(args.params).read_text())
    strategy = _load_strategy(args.strategy, cfg)
    # Node is a contract handle; backtest mode is the only one wired (ADR 0006).
    create_trading_node(mode="backtest", strategy_name=args.strategy)

    if args.source == "polybot_sqlite":
        loader = PolybotSQLiteLoader(db_path=args.polybot_db)
    elif args.source == "paper_ticks":
        loader = PaperTicksLoader(dsn=get_settings().pg_dsn)
    else:
        raise SystemExit(f"source not supported: {args.source}")

    sizing = cfg.get("sizing", {})
    bt = cfg.get("backtest", {})
    fm = cfg.get("fill_model", {})
    fill_cfg = FillConfig(
        slippage_bps=float(fm.get("slippage_bps", 10.0)),
        fill_probability=float(fm.get("fill_probability", 0.95)),
    )
    entry_window = EntryWindowConfig(
        earliest_entry_t_s=int(bt.get("earliest_entry_t_s", 120)),
        latest_entry_t_s=int(bt.get("latest_entry_t_s", 240)),
    )
    risk_manager = RiskManager({"risk": cfg.get("risk", {})})

    stake = min(
        float(sizing.get("stake_usd", 3.0)),
        float(cfg.get("risk", {}).get("max_position_size_usd", 5.0)),
    )

    result = run_backtest(
        strategy=strategy,
        loader=loader,
        from_ts=args.from_ts.timestamp(),
        to_ts=args.to_ts.timestamp(),
        stake_usd=stake,
        fill_cfg=fill_cfg,
        entry_window=entry_window,
        risk_manager=risk_manager,
        config_used=cfg,
        seed=args.seed,
    )

    log.info(
        "backtest.done",
        strategy=args.strategy,
        n_markets=result.n_markets,
        n_ticks=result.n_ticks,
        n_trades=result.n_trades,
    )

    if args.no_persist:
        log.info("report.persist.skipped", reason="--no-persist")
        return

    settings = get_settings()
    backtest_id, report_path = await persist_and_render(
        result=result,
        dsn=settings.pg_dsn,
        strategy_name=args.strategy,
        params=cfg,
        data_source=args.source,
    )
    log.info("report.persist.done", backtest_id=backtest_id, path=str(report_path))


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.backtest")
    p.add_argument("--strategy", required=True)
    p.add_argument("--params", required=True)
    p.add_argument("--from", dest="from_ts", required=True, type=_parse_ts)
    p.add_argument("--to", dest="to_ts", required=True, type=_parse_ts)
    p.add_argument(
        "--source",
        default="polybot_sqlite",
        choices=["polybot_sqlite", "paper_ticks"],
        help="polybot_sqlite: read from /home/coder/polybot-btc5m (parity). "
        "paper_ticks: read from market_data.paper_ticks (Phase 3 output).",
    )
    p.add_argument("--polybot-db", default="/polybot-btc5m-data/polybot.db")
    p.add_argument("--no-persist", action="store_true", help="skip DB + HTML (smoke test)")
    p.add_argument("--seed", type=int, default=42, help="deterministic fill-sim RNG seed")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
