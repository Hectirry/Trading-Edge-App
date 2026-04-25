"""Backtest CLI.

Usage:
  python -m trading.cli.backtest \
    --strategy polymarket_btc5m/trend_confirm_t1_v1 \
    --params config/strategies/pbt5m_trend_confirm_t1_v1.toml \
    --from 2026-04-17T15:05:19Z --to 2026-04-21T23:59:59Z \
    --source polybot_sqlite \
    --polybot-db /polybot-btc5m-data/polybot.db
"""

from __future__ import annotations

import argparse
import asyncio
import tomllib as tomli
from datetime import UTC, datetime
from pathlib import Path

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger
from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, run_backtest
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.node import create_trading_node
from trading.engine.risk import RiskManager
from trading.paper.backtest_loader import PaperTicksLoader
from trading.research.report import persist_and_render
from trading.strategies.polymarket_btc5m._macro_provider import Candle, FixedMacroProvider

log = get_logger("cli.backtest")


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


async def _load_macro_provider(*, from_ts: datetime, to_ts: datetime) -> FixedMacroProvider:
    """Pull 5m BTCUSDT candles from market_data.crypto_ohlcv + 34 bars
    of pre-window history so the first queryable minute has a full
    lookback window. Builds a ``FixedMacroProvider`` keyed by bar
    close_ts so strategies can consume it without a DB connection.
    """
    # Pull an extra 34 candles (34 × 5 min = 170 min ≈ 3 h) of lead-in
    # so snapshot_at has a full window at the earliest requested ts.
    lead_in = from_ts.timestamp() - 34 * 300
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, high, low, close FROM market_data.crypto_ohlcv "
            "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m' "
            "AND ts >= to_timestamp($1) AND ts <= $2 ORDER BY ts",
            lead_in,
            to_ts,
        )
    candles = [
        Candle(
            ts=r["ts"].timestamp(),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )
        for r in rows
    ]
    return FixedMacroProvider(candles=candles)


async def _load_strategy(name: str, config: dict, macro_provider):
    if name == "polymarket_btc5m/trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import (
            TrendConfirmT1V1,
        )

        return TrendConfirmT1V1(config=config)
    if name == "polymarket_btc5m/last_90s_forecaster_v1":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v1 import (
            Last90sForecasterV1,
        )

        return Last90sForecasterV1(config, macro_provider=macro_provider)
    if name == "polymarket_btc5m/last_90s_forecaster_v2":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
            Last90sForecasterV2,
            load_runner_async,
        )

        runner = await load_runner_async()
        return Last90sForecasterV2(config, macro_provider=macro_provider, model=runner)
    if name == "polymarket_btc5m/last_90s_forecaster_v3":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            Last90sForecasterV3,
        )
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            load_runner_async as v3_load_runner_async,
        )

        runner = await v3_load_runner_async()
        return Last90sForecasterV3(config, macro_provider=macro_provider, model=runner)
    if name == "polymarket_btc5m/bb_residual_ofi_v1":
        from trading.strategies.polymarket_btc5m.bb_residual_ofi_v1 import (
            BBResidualOFIV1,
        )
        from trading.strategies.polymarket_btc5m.bb_residual_ofi_v1 import (
            load_runner_async as bb_ofi_load_runner_async,
        )

        runner = await bb_ofi_load_runner_async()
        return BBResidualOFIV1(config, model=runner)
    if name == "polymarket_btc5m/contest_ensemble_v1":
        from trading.strategies.polymarket_btc5m.contest_ensemble_v1 import (
            ContestEnsembleV1,
            load_meta_model_async_factory,
        )

        meta_model = await load_meta_model_async_factory()()
        return ContestEnsembleV1(config, macro_provider=macro_provider, meta_model=meta_model)
    if name == "polymarket_btc5m/contest_avengers_v1":
        from trading.strategies.polymarket_btc5m.contest_avengers_v1 import (
            ContestAvengersV1,
        )

        return ContestAvengersV1(config, macro_provider=macro_provider)
    raise SystemExit(f"unknown strategy: {name}")


async def _run(args: argparse.Namespace) -> None:
    cfg = tomli.loads(Path(args.params).read_text())
    macro_provider = await _load_macro_provider(from_ts=args.from_ts, to_ts=args.to_ts)
    strategy = await _load_strategy(args.strategy, cfg, macro_provider)
    # Node is a contract handle; backtest mode is the only one wired (ADR 0006).
    create_trading_node(mode="backtest", strategy_name=args.strategy)

    if args.source == "polybot_sqlite":
        loader = PolybotSQLiteLoader(
            db_path=args.polybot_db,
            slug_encodes_open_ts=args.slug_encodes_open_ts,
        )
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
        bypass_risk=bool(cfg.get("risk", {}).get("bypass_in_backtest", False)),
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
    p.add_argument(
        "--slug-encodes-open-ts",
        action="store_true",
        help="Set for BTC-Tendencia-5m databases where slug = open_ts not close_ts.",
    )
    p.add_argument("--no-persist", action="store_true", help="skip DB + HTML (smoke test)")
    p.add_argument("--seed", type=int, default=42, help="deterministic fill-sim RNG seed")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
