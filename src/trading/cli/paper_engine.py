"""Paper engine entry point — runs inside the tea-engine container in
TRADING_ENV=staging. Spins up feeds, tick recorder, heartbeat publisher,
and the paper driver as one asyncio supervisor."""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from pathlib import Path

import tomli

from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger
from trading.engine.fill_model import FillParams
from trading.engine.node import create_trading_node
from trading.engine.risk import RiskManager
from trading.notifications import telegram as T
from trading.paper.driver import DriverConfig, PaperDriver
from trading.paper.exec_client import SimulatedExecutionClient
from trading.paper.feeds import (
    refresh_markets_loop,
    run_binance_spot_1s,
    run_chainlink_rtds,
    run_clob_l2,
)
from trading.paper.heartbeat import HeartbeatPublisher
from trading.paper.state import FeedState
from trading.paper.tick_recorder import TickRecorder
from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3

log = get_logger("cli.paper_engine")


async def main_async() -> None:
    settings = get_settings()
    staging_path = Path("config/environments/staging.toml")
    staging_cfg = tomli.loads(staging_path.read_text())
    strategy_cfg_path = Path(staging_cfg["strategies"]["imbalance_v3"]["params_file"])
    strategy_cfg = tomli.loads(strategy_cfg_path.read_text())

    # Validate mode wiring — Phase 2 TradingNode factory still applies.
    create_trading_node(mode="paper", strategy_name="polymarket_btc5m/imbalance_v3")
    log.info("paper_engine.starting", env=settings.trading_env)

    capital_usd = float(staging_cfg["env"]["capital_usd"])
    alert_pct = float(staging_cfg["env"]["daily_loss_alert_pct"])
    pause_pct = float(staging_cfg["env"]["daily_loss_pause_pct"])

    strategy = ImbalanceV3(config=strategy_cfg)
    risk = RiskManager({"risk": strategy_cfg["risk"]})
    fill_params = FillParams(
        fee_k=0.05,
        slippage_bps=float(strategy_cfg["fill_model"]["slippage_bps"]),
        fill_probability=float(strategy_cfg["fill_model"]["fill_probability"]),
    )
    exec_client = SimulatedExecutionClient(strategy_id=strategy.name, fill_params=fill_params)

    tg = T.TelegramClient()
    heartbeat = HeartbeatPublisher(
        redis_url=f"redis://{settings.redis_host}:{settings.redis_port}/0",
        interval_s=int(staging_cfg["env"]["heartbeat_interval_s"]),
    )

    state = FeedState()
    tick_recorder = TickRecorder(
        state=state,
        redis_url=f"redis://{settings.redis_host}:{settings.redis_port}/0",
    )

    driver_cfg = DriverConfig(
        strategy_id=strategy.name,
        stake_usd=min(
            float(strategy_cfg["sizing"]["stake_usd"]),
            float(strategy_cfg["risk"]["max_position_size_usd"]),
        ),
        earliest_entry_t=int(strategy_cfg["backtest"]["earliest_entry_t_s"]),
        latest_entry_t=int(strategy_cfg["backtest"]["latest_entry_t_s"]),
        daily_alert_pnl_threshold=-capital_usd * alert_pct,
        daily_pause_pnl_threshold=-capital_usd * pause_pct,
        reconciliation_interval_s=int(staging_cfg["env"]["reconciliation_interval_s"]),
    )
    driver = PaperDriver(
        strategy=strategy,
        risk_manager=risk,
        exec_client=exec_client,
        tg=tg,
        heartbeat=heartbeat,
        cfg=driver_cfg,
        redis_url=f"redis://{settings.redis_host}:{settings.redis_port}/0",
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    tasks = [
        asyncio.create_task(run_binance_spot_1s(state), name="binance_spot"),
        asyncio.create_task(run_chainlink_rtds(state), name="chainlink_rtds"),
        asyncio.create_task(run_clob_l2(state), name="clob_l2"),
        asyncio.create_task(refresh_markets_loop(state), name="market_refresh"),
        asyncio.create_task(tick_recorder.run(), name="tick_recorder"),
        asyncio.create_task(heartbeat.run(), name="heartbeat"),
        asyncio.create_task(driver.run(), name="driver"),
    ]
    await stop_event.wait()
    log.info("paper_engine.stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await tg.aclose()
    log.info("paper_engine.stopped")


def main() -> None:
    configure_logging()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
