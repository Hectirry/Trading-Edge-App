from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger
from trading.common.metrics import start_metrics_server
from trading.ingest.binance import BinanceAdapter
from trading.ingest.bybit import BybitAdapter
from trading.ingest.polymarket import PolymarketAdapter
from trading.ingest.polymarket.slug import SLUG_PREFIX

log = get_logger("cli.ingestor")

INTERVALS = ["1m", "5m", "15m", "1h", "1d"]
CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TRADE_SYMBOLS = ["BTCUSDT"]
POLYMARKET_DISCOVERY_LOOKBACK = timedelta(days=30)


class Supervisor:
    def __init__(self) -> None:
        self.binance = BinanceAdapter()
        self.bybit = BybitAdapter()
        self.polymarket = PolymarketAdapter()
        self._stop = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        tasks = [
            asyncio.create_task(
                self._guarded("binance_ohlcv", self.binance.stream_ohlcv, CRYPTO_SYMBOLS, INTERVALS)
            ),
            asyncio.create_task(
                self._guarded("binance_trades", self.binance.stream_trades, TRADE_SYMBOLS)
            ),
            asyncio.create_task(
                self._guarded("bybit_ohlcv", self.bybit.stream_ohlcv, CRYPTO_SYMBOLS, INTERVALS)
            ),
            asyncio.create_task(
                self._guarded("bybit_trades", self.bybit.stream_trades, TRADE_SYMBOLS)
            ),
            asyncio.create_task(self._guarded("polymarket_loop", self._polymarket_loop)),
            asyncio.create_task(self._guarded("chainlink_updates", self._chainlink_loop)),
            asyncio.create_task(self._guarded("coinalyze_liquidations", self._coinalyze_loop)),
        ]
        log.info("supervisor.started", tasks=len(tasks))

        await self._stop.wait()
        log.info("supervisor.stopping")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            self.binance.aclose(),
            self.bybit.aclose(),
            self.polymarket.aclose(),
            return_exceptions=True,
        )
        log.info("supervisor.stopped")

    async def _guarded(self, name: str, coro_fn, *args) -> None:
        backoff = 10.0
        fails_in_window: list[datetime] = []
        while not self._stop.is_set():
            try:
                await coro_fn(*args)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                now = datetime.now(tz=UTC)
                fails_in_window = [t for t in fails_in_window if now - t < timedelta(minutes=5)]
                fails_in_window.append(now)
                log.error(
                    "supervisor.task.crashed",
                    task=name,
                    err=str(e),
                    fails_last_5min=len(fails_in_window),
                )
                if len(fails_in_window) > 3:
                    log.error("supervisor.task.budget_exceeded", task=name)
                    self._stop.set()
                    return
                await asyncio.sleep(backoff)

    async def _polymarket_loop(self) -> None:
        """One-shot historical pull at startup (30d), then near-real-time top-up every 60s."""
        last_stream_task: asyncio.Task | None = None
        streamed_ids: set[str] = set()
        did_historical_pull = False
        while not self._stop.is_set():
            if not did_historical_pull:
                since = datetime.now(tz=UTC) - POLYMARKET_DISCOVERY_LOOKBACK
                log.info(
                    "polymarket.historical.start", lookback_days=POLYMARKET_DISCOVERY_LOOKBACK.days
                )
                try:
                    n = await self.polymarket.discover_markets(SLUG_PREFIX, since)
                    log.info("polymarket.historical.done", upserted=n)
                    did_historical_pull = True
                except Exception as e:
                    log.warning("polymarket.historical.err", err=str(e))
            else:
                # Near-real-time: last 10 minutes is plenty to catch newly-opened markets.
                near = datetime.now(tz=UTC) - timedelta(minutes=10)
                try:
                    await self.polymarket.discover_markets(SLUG_PREFIX, near)
                except Exception as e:
                    log.warning("polymarket.nrt.err", err=str(e))
            # Get currently open condition_ids.
            async with acquire() as conn:
                rows = await conn.fetch(
                    "SELECT condition_id FROM market_data.polymarket_markets "
                    "WHERE resolved = FALSE AND window_ts >= $1",
                    int(datetime.now(tz=UTC).timestamp()),
                )
            ids = sorted({r["condition_id"] for r in rows})
            if ids and set(ids) != streamed_ids:
                if last_stream_task is not None:
                    last_stream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await last_stream_task
                streamed_ids = set(ids)
                last_stream_task = asyncio.create_task(self.polymarket.stream_prices(ids))
            await asyncio.sleep(60)

    async def _chainlink_loop(self) -> None:
        """Poll Chainlink (Data Streams or Polygon EAC via Alchemy) and
        upsert into market_data.chainlink_updates. No-ops when neither
        key is configured (ADR 0012).
        """
        from trading.ingest.chainlink.adapter import run_chainlink_loop

        await run_chainlink_loop()

    async def _coinalyze_loop(self) -> None:
        """Poll Coinalyze /liquidation-history and upsert into
        market_data.liquidation_clusters. No-ops when key missing.
        """
        from trading.ingest.coinalyze.adapter import run_liquidation_loop

        await run_liquidation_loop()


def main() -> None:
    configure_logging()
    settings = get_settings()
    start_metrics_server(settings.metrics_port)
    log.info(
        "ingestor.startup",
        env=settings.trading_env,
        metrics_port=settings.metrics_port,
    )
    asyncio.run(Supervisor().run())


if __name__ == "__main__":
    main()
