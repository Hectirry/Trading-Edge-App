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


async def _load_hmm_detector():
    """Return an ``HMMRegimeDetector`` for the active ``hmm_regime_btc5m``
    row, else ``NullHMMRegimeDetector``. Strategies that don't need an
    HMM are unaffected.
    """
    from pathlib import Path as _Path

    from trading.common.db import acquire
    from trading.engine.features.hmm_regime import (
        HMMRegimeDetector,
        NullHMMRegimeDetector,
    )

    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT path FROM research.models " "WHERE name = $1 AND is_active = TRUE",
                "hmm_regime_btc5m",
            )
    except Exception as e:
        log.warning("hmm.lookup_err", err=str(e))
        return NullHMMRegimeDetector()
    if row is None:
        log.info("hmm.no_active_row")
        return NullHMMRegimeDetector()
    bundle_path = _Path(row["path"]) / "model.pkl"
    if not bundle_path.exists():
        log.warning("hmm.bundle_missing", path=str(bundle_path))
        return NullHMMRegimeDetector()
    try:
        return HMMRegimeDetector.load(bundle_path)
    except Exception as e:
        log.warning("hmm.load_err", err=str(e))
        return NullHMMRegimeDetector()


async def _shared_providers_refresh_loop(
    chainlink_provider,
    liq_provider,
    macro_provider,
) -> None:
    """Keep the Chainlink + liquidation + macro caches warm.

    Chainlink cache TTL short (5 s), liq clusters medium (30 s), macro
    candles slow (every 5 min). One loop, staggered cadence via
    counters, so we keep a single asyncio task per strategy needs.
    """
    import asyncio as _asyncio

    i = 0
    while True:
        try:
            await chainlink_provider.refresh()
            if i % 6 == 0:
                await liq_provider.refresh()
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
    hmm_detector=None,
    chainlink_provider=None,
    liq_provider=None,
) -> StrategyBase:
    if name == "trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1

        return TrendConfirmT1V1(config=cfg)
    if name == "last_90s_forecaster_v1":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v1 import (
            Last90sForecasterV1,
        )

        return Last90sForecasterV1(cfg, macro_provider=macro_provider)
    if name == "last_90s_forecaster_v2":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
            Last90sForecasterV2,
            load_runner_async,
        )

        runner = await load_runner_async()
        return Last90sForecasterV2(cfg, macro_provider=macro_provider, model=runner)
    if name == "last_90s_forecaster_v3":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            Last90sForecasterV3,
        )
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            load_runner_async as v3_load_runner_async,
        )

        runner = await v3_load_runner_async()
        return Last90sForecasterV3(cfg, macro_provider=macro_provider, model=runner)
    if name == "contest_ensemble_v1":
        from trading.strategies.polymarket_btc5m.contest_ensemble_v1 import (
            ContestEnsembleV1,
            load_meta_model_async_factory,
        )

        meta_model = await load_meta_model_async_factory()()
        return ContestEnsembleV1(
            cfg,
            macro_provider=macro_provider,
            hmm_detector=hmm_detector,
            meta_model=meta_model,
        )
    if name == "contest_avengers_v1":
        from trading.strategies.polymarket_btc5m.contest_avengers_v1 import (
            ContestAvengersV1,
        )

        return ContestAvengersV1(
            cfg,
            macro_provider=macro_provider,
            hmm_detector=hmm_detector,
            chainlink_provider=chainlink_provider,
            liq_provider=liq_provider,
        )
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

    # Shared macro provider for last_90s_forecaster_v1/_v2 + contest_*.
    from trading.strategies.polymarket_btc5m._macro_provider import (
        PostgresMacroProvider,
    )
    from trading.strategies.polymarket_btc5m._shared_providers import (
        CachedChainlinkSnapshot,
        CachedLiqClusters,
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

    # Shared providers for contest_avengers_v1 (and future strategies).
    chainlink_provider = CachedChainlinkSnapshot()
    liq_provider = CachedLiqClusters()
    try:
        await chainlink_provider.refresh()
        await liq_provider.refresh()
    except Exception as e:
        log.warning("paper_engine.shared_providers.refresh_err", err=str(e))

    # Try to load the HMM regime detector from the active
    # research.models row. Returns a NullHMMRegimeDetector if no row
    # exists — strategies degrade cleanly.
    hmm_detector = await _load_hmm_detector()

    # Per-strategy drivers.
    drivers: list[PaperDriver] = []
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
            hmm_detector=hmm_detector,
            chainlink_provider=chainlink_provider,
            liq_provider=liq_provider,
        )
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
        asyncio.create_task(
            _shared_providers_refresh_loop(
                chainlink_provider,
                liq_provider,
                macro_provider,
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
