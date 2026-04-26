"""Kraken Spot WS v2 + REST OHLCV adapter.

Sprint 5 of ADR 0013. Kraken trades XBT/USD (USD nativo — no USDT
basis correction needed for this venue, simplifies the cesta math).

Public endpoints, no auth.
- REST: ``https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1``
  (interval in minutes; max ~720 candles per call).
- WS v2: ``wss://ws.kraken.com/v2`` channels ``ohlc`` and ``trade``.

Kraken's pair naming is special: ``XBTUSD`` (not BTCUSD), and the
REST response also uses ``XXBTZUSD`` legacy keys. We normalise to
canonical ``BTCUSD`` on storage so downstream consumers don't need
to special-case Kraken.
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

# Kraken interval is in minutes (1, 5, 15, 60, 1440).
INTERVAL_TO_KRAKEN_MIN = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "1d": 1440,
}
INTERVAL_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _to_kraken_pair(symbol: str) -> str:
    """BTCUSD → XBTUSD. Idempotent."""
    s = symbol.upper()
    if s.startswith("BTC"):
        return "XBT" + s[3:]
    return s


def _from_kraken_pair(pair: str) -> str:
    """XBTUSD or XXBTZUSD → BTCUSD."""
    p = pair.upper()
    # Strip Kraken legacy 'X' / 'Z' prefixes.
    if p.startswith("XXBT"):
        p = p[1:]
    if p.startswith("XBT"):
        p = "BTC" + p[3:]
    # Strip 'Z' before 4-letter quote currencies.
    if "ZUSD" in p:
        p = p.replace("ZUSD", "USD")
    return p


class KrakenAdapter(CryptoIngestAdapter):
    name = "kraken"
    REST_BASE = "https://api.kraken.com"
    WS_BASE = "wss://ws.kraken.com/v2"

    def __init__(self) -> None:
        self.rate_limiter = AsyncLimiter(max_rate=15, time_period=1.0)
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
    async def _ohlc_page(self, pair: str, interval_min: int, since_s: int) -> list[list[Any]]:
        """Kraken REST returns up to ~720 candles per call. Response shape::

            {
                "result": {
                    "<pair_key>": [[ts, o, h, l, c, vwap, volume, count], ...],
                    "last": <unix>
                }
            }
        """
        async with self.rate_limiter:
            try:
                r = await self._client.get(
                    "/0/public/OHLC",
                    params={"pair": pair, "interval": interval_min, "since": since_s},
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("kraken 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"kraken 5xx: {r.status_code}")
            r.raise_for_status()
            payload = r.json()
            if payload.get("error"):
                raise IngestSourceDown(f"kraken err: {payload['error']}")
            result = payload.get("result", {})
            for k, v in result.items():
                if k == "last":
                    continue
                return v
            return []

    async def backfill_ohlcv(
        self, symbol: str, interval: str, from_ts: datetime, to_ts: datetime
    ) -> int:
        if interval not in INTERVAL_TO_KRAKEN_MIN:
            raise ValueError(f"unknown interval: {interval}")
        pair = _to_kraken_pair(symbol)
        canonical_symbol = symbol.upper()
        interval_min = INTERVAL_TO_KRAKEN_MIN[interval]
        cursor_s = int(from_ts.timestamp())
        end_s = int(to_ts.timestamp())
        total = 0
        # Kraken doesn't accept an explicit `to` — we walk forward by
        # `since` and stop when the page covers ≥ end_s.
        while cursor_s < end_s:
            raw = await self._ohlc_page(pair, interval_min, cursor_s)
            if not raw:
                break
            rows = [
                (
                    self.name,
                    canonical_symbol,
                    interval,
                    datetime.fromtimestamp(int(c[0]), tz=UTC),
                    Decimal(str(c[1])),
                    Decimal(str(c[2])),
                    Decimal(str(c[3])),
                    Decimal(str(c[4])),
                    Decimal(str(c[6])),
                )
                for c in raw
                if int(c[0]) >= cursor_s and int(c[0]) <= end_s
            ]
            if rows:
                n = await upsert_many(
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
                    rows,
                    ["exchange", "symbol", "interval", "ts"],
                )
                total += n
                log.info(
                    "kraken.backfill.page",
                    symbol=symbol,
                    interval=interval,
                    page_rows=n,
                )
            last_s = int(raw[-1][0])
            if last_s <= cursor_s:
                break  # no progress
            cursor_s = last_s + INTERVAL_S[interval]
        return total

    async def backfill_trades(self, symbol: str, from_ts: datetime, to_ts: datetime) -> int:
        log.info("kraken.backfill_trades.skipped", reason="stream-only policy")
        return 0

    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None:
        # Kraken WS v2 supports OHLC subscriptions per (symbol, interval).
        # Multiple intervals → multiple subscribe calls.
        pairs = [_to_kraken_pair(s).replace("USD", "/USD") for s in symbols]
        await self._stream_subs(
            [
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "ohlc",
                        "symbol": pairs,
                        "interval": INTERVAL_TO_KRAKEN_MIN[i],
                    },
                }
                for i in intervals
                if i in INTERVAL_TO_KRAKEN_MIN
            ]
        )

    async def stream_trades(self, symbols: list[str]) -> None:
        pairs = [_to_kraken_pair(s).replace("USD", "/USD") for s in symbols]
        await self._stream_subs(
            [{"method": "subscribe", "params": {"channel": "trade", "symbol": pairs}}]
        )

    async def _stream_subs(self, sub_msgs: list[dict]) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.WS_BASE, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("kraken.ws.connected", n_subs=len(sub_msgs))
                    backoff = 1.0
                    for sm in sub_msgs:
                        await ws.send(json.dumps(sm))
                    async for raw in ws:
                        msg = json.loads(raw)
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        if msg.get("method") in ("subscribe", "pong"):
                            continue
                        chan = msg.get("channel")
                        if chan == "ohlc":
                            await self._handle_ohlc(msg)
                        elif chan == "trade":
                            await self._handle_trade(msg)
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("kraken.ws.disconnect", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_ohlc(self, msg: dict) -> None:
        if msg.get("type") != "update":
            return  # Snapshots have type 'snapshot'; we only persist updates.
        rows = []
        for d in msg.get("data", []):
            # Kraken OHLC update fires per-tick for the in-progress
            # candle and one final fire on close. We only persist on
            # interval boundary close — guarded by upsert_many ON CONFLICT.
            interval_min = int(d.get("interval", 0))
            interval = next(
                (k for k, v in INTERVAL_TO_KRAKEN_MIN.items() if v == interval_min), None
            )
            if interval is None:
                continue
            ts_str = d.get("interval_begin", "")
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            symbol = _from_kraken_pair(d.get("symbol", "").replace("/", ""))
            rows.append(
                (
                    self.name,
                    symbol,
                    interval,
                    ts,
                    Decimal(str(d["open"])),
                    Decimal(str(d["high"])),
                    Decimal(str(d["low"])),
                    Decimal(str(d["close"])),
                    Decimal(str(d.get("volume", 0))),
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
            ts_str = t.get("timestamp", "")
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            side = "buy" if t.get("side") == "buy" else "sell"
            symbol = _from_kraken_pair(t.get("symbol", "").replace("/", ""))
            rows.append(
                (
                    self.name,
                    symbol,
                    ts,
                    str(t["trade_id"]),
                    Decimal(str(t["price"])),
                    Decimal(str(t["qty"])),
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
