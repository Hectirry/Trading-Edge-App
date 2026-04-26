"""Coinbase Advanced Trade API ingest adapter.

Minimal viable surface for ADR 0013 Sprint 2:

- Backfill 1m / 5m / 1h / 1d OHLCV via REST ``/products/{id}/candles``.
- Stream live ``ticker`` (best bid/ask) → translated into a synthetic
  1m OHLCV row only on ``confirm`` boundaries (matches the Bybit /
  Binance pattern of writing closed candles).
- Stream live ``market_trades`` → ``market_data.crypto_trades`` rows
  with ``exchange='coinbase'``.

Public market-data endpoints don't require auth or API keys; the
upgrade to the new "Advanced Trade" host (``advanced-trade-ws.coinbase.com``)
in 2024 unified the prior Coinbase Pro / Exchange feeds. Trades older
than ~2 weeks are NOT served by the public REST trades endpoint —
backfill_trades is therefore a stream-only no-op (same policy as Bybit).

Product IDs: Coinbase uses ``BTC-USD`` (USD nativo, no USDT). The
adapter accepts our canonical ``BTCUSD`` symbol shape and translates.
"""

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

# Coinbase REST granularity is in seconds.
INTERVAL_TO_GRANULARITY = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}
INTERVAL_S = {k: v for k, v in INTERVAL_TO_GRANULARITY.items()}


def _to_product_id(symbol: str) -> str:
    """Map BTCUSD → BTC-USD. Idempotent if already hyphenated."""
    s = symbol.upper()
    if "-" in s:
        return s
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    return s


class CoinbaseAdapter(CryptoIngestAdapter):
    name = "coinbase"
    REST_BASE = "https://api.exchange.coinbase.com"
    WS_BASE = "wss://advanced-trade-ws.coinbase.com"

    def __init__(self) -> None:
        # Coinbase public REST: 10 req/s sustained; WS no rate limit
        # beyond 750 subscriptions per connection.
        self.rate_limiter = AsyncLimiter(max_rate=8, time_period=1.0)
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
    async def _candles_page(
        self, product_id: str, granularity: int, start_s: int, end_s: int
    ) -> list[list[Any]]:
        """One REST page. Coinbase returns up to 300 candles per call,
        ordered NEWEST-first as ``[time, low, high, open, close, volume]``.
        """
        async with self.rate_limiter:
            try:
                r = await self._client.get(
                    f"/products/{product_id}/candles",
                    params={
                        "granularity": granularity,
                        "start": datetime.fromtimestamp(start_s, tz=UTC).isoformat(),
                        "end": datetime.fromtimestamp(end_s, tz=UTC).isoformat(),
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("coinbase 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"coinbase 5xx: {r.status_code}")
            r.raise_for_status()
            payload = r.json()
            # Coinbase returns newest-first; we ascend.
            return list(reversed(payload))

    async def backfill_ohlcv(
        self, symbol: str, interval: str, from_ts: datetime, to_ts: datetime
    ) -> int:
        if interval not in INTERVAL_TO_GRANULARITY:
            raise ValueError(f"unknown interval: {interval}")
        product = _to_product_id(symbol)
        granularity = INTERVAL_TO_GRANULARITY[interval]
        # Page = 300 candles. step_s spans <300 candles to be safe.
        page_span = 290 * granularity
        cursor = int(from_ts.timestamp())
        end_s = int(to_ts.timestamp())
        total = 0
        while cursor < end_s:
            page_end = min(cursor + page_span, end_s)
            raw = await self._candles_page(product, granularity, cursor, page_end)
            if not raw:
                cursor = page_end + 1
                continue
            rows = [
                (
                    self.name,
                    symbol.upper(),
                    interval,
                    datetime.fromtimestamp(int(c[0]), tz=UTC),
                    Decimal(str(c[3])),  # open
                    Decimal(str(c[2])),  # high
                    Decimal(str(c[1])),  # low
                    Decimal(str(c[4])),  # close
                    Decimal(str(c[5])),  # volume
                )
                for c in raw
            ]
            n = await upsert_many(
                "market_data.crypto_ohlcv",
                ["exchange", "symbol", "interval", "ts", "open", "high", "low", "close", "volume"],
                rows,
                ["exchange", "symbol", "interval", "ts"],
            )
            total += n
            last_open_s = int(raw[-1][0])
            cursor = last_open_s + granularity
            log.info(
                "coinbase.backfill.page",
                symbol=symbol,
                interval=interval,
                page_rows=n,
                cursor=datetime.fromtimestamp(cursor, tz=UTC).isoformat(),
            )
        return total

    async def backfill_trades(self, symbol: str, from_ts: datetime, to_ts: datetime) -> int:
        # Public REST trades endpoint only exposes the most recent ~2 weeks
        # via cursor pagination, with no easy time-range filter. Bybit /
        # Binance follow the same stream-only policy; we mirror it.
        log.info("coinbase.backfill_trades.skipped", reason="stream-only policy")
        return 0

    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None:
        # Coinbase advanced-trade WS only supports 1m candles via the
        # ``candles`` channel; longer intervals must be derived from
        # 1 m via Timescale continuous aggregates. For Sprint 2 we only
        # stream 1 m and warn if other intervals were requested.
        if any(i != "1m" for i in intervals):
            skipped = [i for i in intervals if i != "1m"]
            log.warning(
                "coinbase.stream_ohlcv.skipped_intervals",
                intervals=skipped,
                reason="WS supports 1m candles only — derive longer intervals from 1m",
            )
        products = [_to_product_id(s) for s in symbols]
        await self._stream_channel("candles", products)

    async def stream_trades(self, symbols: list[str]) -> None:
        products = [_to_product_id(s) for s in symbols]
        await self._stream_channel("market_trades", products)

    async def _stream_channel(self, channel: str, product_ids: list[str]) -> None:
        backoff = 1.0
        sub_msg = json.dumps(
            {
                "type": "subscribe",
                "product_ids": product_ids,
                "channel": channel,
            }
        )
        while True:
            try:
                async with websockets.connect(
                    self.WS_BASE, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info(
                        "coinbase.ws.connected",
                        channel=channel,
                        n_products=len(product_ids),
                    )
                    backoff = 1.0
                    await ws.send(sub_msg)
                    async for raw in ws:
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        msg = json.loads(raw)
                        if msg.get("channel") == "subscriptions":
                            continue
                        if msg.get("channel") == "candles":
                            await self._handle_candles(msg)
                        elif msg.get("channel") == "market_trades":
                            await self._handle_trades(msg)
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("coinbase.ws.disconnect", channel=channel, err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_candles(self, msg: dict) -> None:
        rows = []
        for event in msg.get("events", []):
            for c in event.get("candles", []):
                # Advanced-trade ``candles`` events only fire on close
                # (no in-progress mid updates), so every row is final.
                ts = datetime.fromtimestamp(int(c["start"]), tz=UTC)
                product = c["product_id"]
                # Map BTC-USD → BTCUSD canonical
                symbol = product.replace("-", "")
                rows.append(
                    (
                        self.name,
                        symbol,
                        "1m",
                        ts,
                        Decimal(str(c["open"])),
                        Decimal(str(c["high"])),
                        Decimal(str(c["low"])),
                        Decimal(str(c["close"])),
                        Decimal(str(c["volume"])),
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

    async def _handle_trades(self, msg: dict) -> None:
        rows = []
        for event in msg.get("events", []):
            for t in event.get("trades", []):
                # Coinbase uses 'BUY' / 'SELL' for taker side.
                side = "buy" if t.get("side", "").upper() == "BUY" else "sell"
                # `time` is ISO8601 with Z suffix.
                ts_str = t.get("time", "")
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                symbol = t["product_id"].replace("-", "")
                rows.append(
                    (
                        self.name,
                        symbol,
                        ts,
                        str(t["trade_id"]),
                        Decimal(str(t["price"])),
                        Decimal(str(t["size"])),
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
