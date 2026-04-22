from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import websockets
from aiolimiter import AsyncLimiter
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from trading.common.db import upsert_many
from trading.common.logging import get_logger
from trading.common.metrics import REGISTRY
from trading.ingest.base import (
    CryptoIngestAdapter,
    HealthStatus,
    IngestRateLimitError,
    IngestSourceDown,
)

log = get_logger(__name__)

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "1d": 86_400_000,
}


class BinanceAdapter(CryptoIngestAdapter):
    name = "binance"
    REST_BASE = "https://api.binance.com"
    WS_BASE = "wss://stream.binance.com:9443"

    def __init__(self) -> None:
        # Public weight budget is 1200/min; stay well under.
        self.rate_limiter = AsyncLimiter(max_rate=20, time_period=1.0)
        self._client = httpx.AsyncClient(base_url=self.REST_BASE, timeout=20.0)
        self._last_msg_ts: datetime | None = None
        self._last_error: str | None = None
        self._msg_counter = REGISTRY.counter(
            "tea_ingest_messages_total", "messages received", {"adapter": self.name}
        )
        self._err_counter = REGISTRY.counter(
            "tea_ingest_errors_total", "errors raised", {"adapter": self.name}
        )
        self._age_gauge = REGISTRY.gauge(
            "tea_ingest_last_message_age_seconds",
            "seconds since last stream message",
            {"adapter": self.name},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _klines_page(
        self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000
    ) -> list[list[Any]]:
        async with self.rate_limiter:
            try:
                r = await self._client.get(
                    "/api/v3/klines",
                    params={
                        "symbol": symbol.upper(),
                        "interval": interval,
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": limit,
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429 or r.status_code == 418:
                raise IngestRateLimitError(f"binance {r.status_code}")
            if r.status_code >= 500:
                raise IngestSourceDown(f"binance 5xx: {r.status_code}")
            r.raise_for_status()
            return r.json()

    async def backfill_ohlcv(
        self, symbol: str, interval: str, from_ts: datetime, to_ts: datetime
    ) -> int:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unknown interval: {interval}")
        step_ms = INTERVAL_MS[interval] * 1000  # 1000 candles per page
        start_ms = int(from_ts.timestamp() * 1000)
        end_ms = int(to_ts.timestamp() * 1000)
        total = 0
        cursor = start_ms
        while cursor < end_ms:
            page_end = min(cursor + step_ms, end_ms)
            raw = await self._klines_page(symbol, interval, cursor, page_end, limit=1000)
            if not raw:
                cursor = page_end + 1
                continue
            rows = [
                (
                    self.name,
                    symbol.upper(),
                    interval,
                    datetime.fromtimestamp(k[0] / 1000, tz=UTC),
                    Decimal(k[1]),
                    Decimal(k[2]),
                    Decimal(k[3]),
                    Decimal(k[4]),
                    Decimal(k[5]),
                )
                for k in raw
            ]
            n = await upsert_many(
                "market_data.crypto_ohlcv",
                ["exchange", "symbol", "interval", "ts", "open", "high", "low", "close", "volume"],
                rows,
                ["exchange", "symbol", "interval", "ts"],
            )
            total += n
            last_open_ms = raw[-1][0]
            cursor = last_open_ms + INTERVAL_MS[interval]
            log.info(
                "binance.backfill.page",
                symbol=symbol,
                interval=interval,
                page_rows=n,
                cursor=datetime.fromtimestamp(cursor / 1000, tz=UTC).isoformat(),
            )
        return total

    async def backfill_trades(self, symbol: str, from_ts: datetime, to_ts: datetime) -> int:
        # Phase 1 decision: stream-only, no historical backfill for tick trades.
        # See plan approved 2026-04-22.
        log.info("binance.backfill_trades.skipped", reason="stream-only policy")
        return 0

    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None:
        streams = [f"{s.lower()}@kline_{i}" for s in symbols for i in intervals]
        url = f"{self.WS_BASE}/stream?streams={'/'.join(streams)}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    log.info("binance.ws.connected", kind="kline", streams=len(streams))
                    backoff = 1.0
                    async for raw in ws:
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        k = data.get("k")
                        if not k or not k.get("x"):
                            # only write closed candles
                            continue
                        ts = datetime.fromtimestamp(k["t"] / 1000, tz=UTC)
                        row = (
                            self.name,
                            data["s"],
                            k["i"],
                            ts,
                            Decimal(k["o"]),
                            Decimal(k["h"]),
                            Decimal(k["l"]),
                            Decimal(k["c"]),
                            Decimal(k["v"]),
                        )
                        await upsert_many(
                            "market_data.crypto_ohlcv",
                            [
                                "exchange",
                                "symbol",
                                "interval",
                                "ts",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                            ],
                            [row],
                            ["exchange", "symbol", "interval", "ts"],
                        )
                        self._msg_counter.inc()
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("binance.ws.disconnect", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def stream_trades(self, symbols: list[str]) -> None:
        streams = [f"{s.lower()}@aggTrade" for s in symbols]
        url = f"{self.WS_BASE}/stream?streams={'/'.join(streams)}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    log.info("binance.ws.connected", kind="trade", streams=len(streams))
                    backoff = 1.0
                    buffer: list[tuple] = []
                    last_flush = datetime.now(tz=UTC)
                    async for raw in ws:
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        ts = datetime.fromtimestamp(data["T"] / 1000, tz=UTC)
                        side = "sell" if data.get("m") else "buy"
                        buffer.append(
                            (
                                self.name,
                                data["s"],
                                ts,
                                str(data["a"]),
                                Decimal(data["p"]),
                                Decimal(data["q"]),
                                side,
                            )
                        )
                        now = datetime.now(tz=UTC)
                        if len(buffer) >= 500 or (now - last_flush) >= timedelta(seconds=5):
                            await upsert_many(
                                "market_data.crypto_trades",
                                [
                                    "exchange",
                                    "symbol",
                                    "ts",
                                    "trade_id",
                                    "price",
                                    "qty",
                                    "side",
                                ],
                                buffer,
                                ["exchange", "symbol", "trade_id", "ts"],
                            )
                            self._msg_counter.inc(len(buffer))
                            buffer = []
                            last_flush = now
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("binance.ws.disconnect", kind="trade", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def health(self) -> HealthStatus:
        if self._last_msg_ts is None:
            return HealthStatus(False, None, self._last_error, 0.0)
        age = (datetime.now(tz=UTC) - self._last_msg_ts).total_seconds()
        self._age_gauge.set(age)
        return HealthStatus(
            alive=age < 60,
            last_message_ts=self._last_msg_ts,
            last_error=self._last_error,
            messages_per_min=0.0,
        )
