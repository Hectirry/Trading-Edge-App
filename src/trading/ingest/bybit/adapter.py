from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
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

# Bybit v5 uses minute-number strings for intervals. Map to our canonical names.
INTERVAL_TO_BYBIT = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "1d": "D",
}
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}


class BybitAdapter(CryptoIngestAdapter):
    name = "bybit"
    REST_BASE = "https://api.bybit.com"
    WS_BASE = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self) -> None:
        self.rate_limiter = AsyncLimiter(max_rate=30, time_period=1.0)
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
                    "/v5/market/kline",
                    params={
                        "category": "spot",
                        "symbol": symbol.upper(),
                        "interval": INTERVAL_TO_BYBIT[interval],
                        "start": start_ms,
                        "end": end_ms,
                        "limit": limit,
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("bybit 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"bybit 5xx: {r.status_code}")
            r.raise_for_status()
            payload = r.json()
            if payload.get("retCode") != 0:
                raise IngestSourceDown(
                    f"bybit retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}"
                )
            # Bybit returns rows newest-first. Reverse for ascending processing.
            return list(reversed(payload["result"]["list"]))

    async def backfill_ohlcv(
        self, symbol: str, interval: str, from_ts: datetime, to_ts: datetime
    ) -> int:
        if interval not in INTERVAL_TO_BYBIT:
            raise ValueError(f"unknown interval: {interval}")
        step_ms = INTERVAL_MS[interval] * 1000
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
                    datetime.fromtimestamp(int(k[0]) / 1000, tz=UTC),
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
            last_open_ms = int(raw[-1][0])
            cursor = last_open_ms + INTERVAL_MS[interval]
            log.info(
                "bybit.backfill.page",
                symbol=symbol,
                interval=interval,
                page_rows=n,
                cursor=datetime.fromtimestamp(cursor / 1000, tz=UTC).isoformat(),
            )
        return total

    async def backfill_trades(self, symbol: str, from_ts: datetime, to_ts: datetime) -> int:
        log.info("bybit.backfill_trades.skipped", reason="stream-only policy")
        return 0

    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None:
        topics = [f"kline.{INTERVAL_TO_BYBIT[i]}.{s.upper()}" for s in symbols for i in intervals]
        await self._stream_topics(topics, kind="kline")

    async def stream_trades(self, symbols: list[str]) -> None:
        topics = [f"publicTrade.{s.upper()}" for s in symbols]
        await self._stream_topics(topics, kind="trade")

    async def _stream_topics(self, topics: list[str], kind: str) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.WS_BASE, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("bybit.ws.connected", kind=kind, topics=len(topics))
                    backoff = 1.0
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            self._last_msg_ts = datetime.now(tz=UTC)
                            self._age_gauge.set(0.0)
                            msg = json.loads(raw)
                            if msg.get("op") in ("subscribe", "pong"):
                                continue
                            topic = msg.get("topic", "")
                            if topic.startswith("kline."):
                                await self._handle_kline(msg)
                            elif topic.startswith("publicTrade."):
                                await self._handle_trade(msg)
                    finally:
                        heartbeat_task.cancel()
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("bybit.ws.disconnect", kind=kind, err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                return

    async def _handle_kline(self, msg: dict) -> None:
        # topic: kline.<interval>.<symbol>
        _, interval_bybit, symbol = msg["topic"].split(".")
        interval = next(k for k, v in INTERVAL_TO_BYBIT.items() if v == interval_bybit)
        rows = []
        for k in msg.get("data", []):
            if not k.get("confirm"):
                continue
            ts = datetime.fromtimestamp(int(k["start"]) / 1000, tz=UTC)
            rows.append(
                (
                    self.name,
                    symbol,
                    interval,
                    ts,
                    Decimal(str(k["open"])),
                    Decimal(str(k["high"])),
                    Decimal(str(k["low"])),
                    Decimal(str(k["close"])),
                    Decimal(str(k["volume"])),
                )
            )
        if rows:
            await upsert_many(
                "market_data.crypto_ohlcv",
                ["exchange", "symbol", "interval", "ts", "open", "high", "low", "close", "volume"],
                rows,
                ["exchange", "symbol", "interval", "ts"],
            )
            self._msg_counter.inc(len(rows))

    async def _handle_trade(self, msg: dict) -> None:
        rows = []
        for t in msg.get("data", []):
            ts = datetime.fromtimestamp(int(t["T"]) / 1000, tz=UTC)
            side = "buy" if t.get("S") == "Buy" else "sell"
            rows.append(
                (
                    self.name,
                    t["s"],
                    ts,
                    str(t["i"]),
                    Decimal(str(t["p"])),
                    Decimal(str(t["v"])),
                    side,
                )
            )
        if rows:
            await upsert_many(
                "market_data.crypto_trades",
                ["exchange", "symbol", "ts", "trade_id", "price", "qty", "side"],
                rows,
                ["exchange", "symbol", "trade_id", "ts"],
            )
            self._msg_counter.inc(len(rows))

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
