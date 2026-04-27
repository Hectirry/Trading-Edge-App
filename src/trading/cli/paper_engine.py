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


async def _shared_providers_refresh_loop(
    macro_provider,
    microstructure_provider=None,
    oracle_lag_cestas: list | None = None,
) -> None:
    """Keep shared caches warm.

    - Microstructure: every 5 s (matches the loop tick).
    - Oracle-lag cestas: every 60 s (Coinbase / OKX / Bybit / Kraken
      OHLCV is 1m so refresh more often is wasted DB load).
    - Macro candles: every 5 min.
    """
    import asyncio as _asyncio

    from trading.common.db import acquire

    i = 0
    while True:
        try:
            if microstructure_provider is not None:
                await microstructure_provider.refresh()
            if oracle_lag_cestas and i % 12 == 0:
                async with acquire() as conn:
                    for cesta in oracle_lag_cestas:
                        try:
                            await cesta.refresh(conn)
                        except Exception as e:
                            log.warning("oracle_lag.cesta.refresh_err", err=str(e))
            if i % 60 == 0:
                await macro_provider.refresh(hours=6)
        except Exception as e:
            log.warning("shared_providers.refresh_err", err=str(e))
        i += 1
        await _asyncio.sleep(5)


async def _load_strategy(
    name: str,
    cfg: dict,
    macro_provider=None,
    *,
    microstructure_provider=None,
) -> StrategyBase:
    if name == "trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1

        return TrendConfirmT1V1(config=cfg)
    if name == "oracle_lag_v1":
        from trading.cli.backtest import _load_oracle_lag_cesta
        from trading.strategies.polymarket_btc5m.oracle_lag_v1 import OracleLagV1

        cesta = await _load_oracle_lag_cesta(cfg)
        return OracleLagV1(config=cfg, cesta=cesta)
    if name == "last_90s_forecaster_v3":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            Last90sForecasterV3,
        )
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            load_runner_async as v3_load_runner_async,
        )

        runner = await v3_load_runner_async()
        return Last90sForecasterV3(
            cfg,
            macro_provider=macro_provider,
            model=runner,
            microstructure_provider=microstructure_provider,
        )
    if name == "mm_rebate_v1":
        # Step 2 — first MM-style strategy. Uses on_tick + limit_book_sim
        # via MMPaperDriver, NOT the standard ENTER flow.
        from trading.strategies.polymarket_btc15m._k_estimator import KEstimator
        from trading.strategies.polymarket_btc15m.mm_rebate_v1 import MMRebateV1

        k_est = KEstimator(strategy_id="mm_rebate_v1")
        # Warm-start from Step 0 v1 nominee bucket (parametrizable later).
        k_est.warm_start("0.15-0.20", 2, k0=37.4, minutes=60.0)
        try:
            await k_est.load_from_db()
        except Exception:
            pass  # tolerate empty/no state at first boot
        return MMRebateV1(config=cfg, k_estimator=k_est)
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

    # Shared macro provider — used by last_90s_forecaster_v3.
    from trading.strategies.polymarket_btc5m._macro_provider import (
        PostgresMacroProvider,
    )

    macro_provider = PostgresMacroProvider()
    try:
        await macro_provider.refresh(hours=6)
        log.info(
            "paper_engine.macro_provider.ready",
            n_candles=len(macro_provider._cache),
        )
    except Exception as e:
        log.warning("paper_engine.macro_provider.refresh_err", err=str(e))

    # v3 microstructure cache. Refreshed every 5 s in
    # _shared_providers_refresh_loop. fetch() is sync (called from
    # should_enter); cache is fed by an async refresh that pulls the
    # last 90 s of crypto_trades.
    from trading.strategies.polymarket_btc5m._microstructure_provider import (
        PostgresMicrostructureProvider,
    )

    microstructure_provider = PostgresMicrostructureProvider()
    try:
        await microstructure_provider.refresh()
        log.info("paper_engine.microstructure_provider.ready")
    except Exception as e:
        log.warning("paper_engine.microstructure_provider.refresh_err", err=str(e))

    # Per-strategy drivers. Track any cesta providers so the shared
    # refresh loop can keep their per-venue caches warm.
    drivers: list[PaperDriver] = []
    oracle_lag_cestas: list = []
    strategies_cfg = staging_cfg.get("strategies", {})
    for name, entry in strategies_cfg.items():
        if not entry.get("enabled"):
            log.info("paper_engine.strategy.disabled", name=name)
            continue
        strategy_cfg = tomli.loads(Path(entry["params_file"]).read_text())
        strategy = await _load_strategy(
            name,
            strategy_cfg,
            macro_provider=macro_provider,
            microstructure_provider=microstructure_provider,
        )
        if (
            name == "oracle_lag_v1"
            and getattr(strategy, "cesta", None) is not None
        ):
            oracle_lag_cestas.append(strategy.cesta)
        risk = RiskManager({"risk": strategy_cfg["risk"]})
        fill_params = FillParams(
            fee_k=0.05,
            slippage_bps=float(strategy_cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(strategy_cfg["fill_model"]["fill_probability"]),
        )
        exec_client = SimulatedExecutionClient(strategy_id=strategy.name, fill_params=fill_params)
        # Strategy class decides which driver to spawn. MM-style strategies
        # (override `on_tick`) get an MMPaperDriver bound to a LimitBookSim;
        # direction-style strategies keep the standard PaperDriver.
        is_mm_strategy = (
            type(strategy).on_tick is not StrategyBase.on_tick
        )
        if is_mm_strategy:
            from trading.paper.limit_book_sim import LimitBookSim
            from trading.paper.mm_driver import MMPaperDriver

            limit_book = LimitBookSim(mode="paper")
            driver = MMPaperDriver(
                strategy=strategy,
                risk_manager=risk,
                exec_client=exec_client,
                tg=tg,
                heartbeat=heartbeat,
                cfg=_driver_config(name, strategy_cfg, staging_cfg),
                redis_url=redis_url,
                limit_book=limit_book,
            )
        else:
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
        log.info("paper_engine.strategy.enabled", name=name, is_mm=is_mm_strategy)

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
        asyncio.create_task(
            _shared_providers_refresh_loop(
                macro_provider,
                microstructure_provider=microstructure_provider,
                oracle_lag_cestas=oracle_lag_cestas,
            ),
            name="shared_providers_refresh",
        ),
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
