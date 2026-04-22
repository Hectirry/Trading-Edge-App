"""Paper engine entry point — multi-strategy supervisor (ADR 0008).

Reads the `[strategies.*]` registry from `config/environments/staging.toml`.
For each enabled strategy, spawns a PaperDriver + RiskManager +
SimulatedExecutionClient with per-strategy capital/thresholds. Feeds,
tick recorder, heartbeat publisher, and Telegram client are singletons.
"""

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
from trading.engine.strategy_base import StrategyBase
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

log = get_logger("cli.paper_engine")


def _load_strategy(name: str, cfg: dict) -> StrategyBase:
    if name == "imbalance_v3":
        from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3

        return ImbalanceV3(config=cfg)
    if name == "trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1

        return TrendConfirmT1V1(config=cfg)
    raise RuntimeError(f"unknown strategy: {name}")


def _driver_config(strategy_name: str, strategy_cfg: dict, env_cfg: dict) -> DriverConfig:
    paper = strategy_cfg.get("paper", {})
    capital = float(paper.get("capital_usd", env_cfg["env"]["capital_usd"]))
    alert_pct = float(paper.get("daily_loss_alert_pct", env_cfg["env"]["daily_loss_alert_pct"]))
    pause_pct = float(paper.get("daily_loss_pause_pct", env_cfg["env"]["daily_loss_pause_pct"]))
    stake = min(
        float(strategy_cfg["sizing"]["stake_usd"]),
        float(strategy_cfg["risk"]["max_position_size_usd"]),
    )
    return DriverConfig(
        strategy_id=strategy_name,
        stake_usd=stake,
        earliest_entry_t=int(strategy_cfg["backtest"]["earliest_entry_t_s"]),
        latest_entry_t=int(strategy_cfg["backtest"]["latest_entry_t_s"]),
        daily_alert_pnl_threshold=-capital * alert_pct,
        daily_pause_pnl_threshold=-capital * pause_pct,
        reconciliation_interval_s=int(env_cfg["env"]["reconciliation_interval_s"]),
    )


async def main_async() -> None:
    settings = get_settings()
    staging_cfg = tomli.loads(Path("config/environments/staging.toml").read_text())
    redis_url = f"redis://{settings.redis_host}:{settings.redis_port}/0"

    create_trading_node(mode="paper", strategy_name="multi")
    log.info("paper_engine.starting", env=settings.trading_env)

    # Shared infrastructure (singletons).
    state = FeedState()
    tick_recorder = TickRecorder(state=state, redis_url=redis_url)
    heartbeat = HeartbeatPublisher(
        redis_url=redis_url, interval_s=int(staging_cfg["env"]["heartbeat_interval_s"])
    )
    tg = T.TelegramClient()

    # Per-strategy drivers.
    drivers: list[PaperDriver] = []
    strategies_cfg = staging_cfg.get("strategies", {})
    for name, entry in strategies_cfg.items():
        if not entry.get("enabled"):
            log.info("paper_engine.strategy.disabled", name=name)
            continue
        strategy_cfg = tomli.loads(Path(entry["params_file"]).read_text())
        strategy = _load_strategy(name, strategy_cfg)
        risk = RiskManager({"risk": strategy_cfg["risk"]})
        fill_params = FillParams(
            fee_k=0.05,
            slippage_bps=float(strategy_cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(strategy_cfg["fill_model"]["fill_probability"]),
        )
        exec_client = SimulatedExecutionClient(strategy_id=strategy.name, fill_params=fill_params)
        driver = PaperDriver(
            strategy=strategy,
            risk_manager=risk,
            exec_client=exec_client,
            tg=tg,
            heartbeat=heartbeat,
            cfg=_driver_config(name, strategy_cfg, staging_cfg),
            redis_url=redis_url,
        )
        drivers.append(driver)
        log.info("paper_engine.strategy.enabled", name=name)

    if not drivers:
        log.error("paper_engine.no_strategies_enabled")
        return

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
    ]
    for d in drivers:
        tasks.append(asyncio.create_task(d.run(), name=f"driver_{d.strategy.name}"))

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
