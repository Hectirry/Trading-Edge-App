"""OKX V5 public adapter — REST candles backfill + WS streams.

Sprint 5 of ADR 0013 (oracle_lag_v1 cesta). OKX trades BTC-USDT (USDT-
denominated, same USDT basis correction as Binance applies).

Public endpoints — no auth required for market data.
- REST: ``https://www.okx.com/api/v5/market/candles`` (max 300 / call,
  ``bar`` ∈ {1m, 5m, 15m, 1H, 1D, …}).
- WS: ``wss://ws.okx.com:8443/ws/v5/public`` channels ``candle1m`` /
  ``trades``.
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

# OKX uses 1m, 5m, 15m, 1H, 1D for the `bar` query parameter.
INTERVAL_TO_BAR = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "1d": "1D",
}
INTERVAL_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _to_okx_inst_id(symbol: str) -> str:
    """BTCUSDT → BTC-USDT. Idempotent."""
    s = symbol.upper()
    if "-" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return s


class OkxAdapter(CryptoIngestAdapter):
    name = "okx"
    REST_BASE = "https://www.okx.com"
    WS_BASE = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self) -> None:
        self.rate_limiter = AsyncLimiter(max_rate=20, time_period=2.0)
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
        self, inst_id: str, bar: str, before_ms: int, limit: int = 300
    ) -> list[list[Any]]:
        """OKX returns up to 300 candles, NEWEST first as
        ``[ts_ms, o, h, l, c, vol, vol_ccy, vol_quote, confirm]``.

        OKX V5 ``after`` semantics: returns records OLDER than the
        ts (exclusive). The parameter name in our caller is
        ``before_ms`` for caller readability; we send it as the
        ``after`` query param.
        """
        async with self.rate_limiter:
            try:
                params = {"instId": inst_id, "bar": bar, "limit": limit}
                if before_ms:
                    params["after"] = before_ms
                r = await self._client.get("/api/v5/market/history-candles", params=params)
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("okx 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"okx 5xx: {r.status_code}")
            r.raise_for_status()
            payload = r.json()
            if payload.get("code") != "0":
                raise IngestSourceDown(
                    f"okx code={payload.get('code')} msg={payload.get('msg')}"
                )
            # NEWEST first → reverse for ascending processing.
            return list(reversed(payload.get("data", [])))

    async def backfill_ohlcv(
        self, symbol: str, interval: str, from_ts: datetime, to_ts: datetime
    ) -> int:
        if interval not in INTERVAL_TO_BAR:
            raise ValueError(f"unknown interval: {interval}")
        inst_id = _to_okx_inst_id(symbol)
        bar = INTERVAL_TO_BAR[interval]
        step_ms = INTERVAL_S[interval] * 1000
        end_ms = int(to_ts.timestamp() * 1000)
        cursor_ms = end_ms
        from_ms = int(from_ts.timestamp() * 1000)
        total = 0
        while cursor_ms > from_ms:
            raw = await self._candles_page(inst_id, bar, before_ms=cursor_ms, limit=300)
            if not raw:
                break
            rows = [
                (
                    self.name,
                    symbol.upper(),
                    interval,
                    datetime.fromtimestamp(int(c[0]) / 1000, tz=UTC),
                    Decimal(str(c[1])),
                    Decimal(str(c[2])),
                    Decimal(str(c[3])),
                    Decimal(str(c[4])),
                    Decimal(str(c[5])),
                )
                for c in raw
                if int(c[0]) >= from_ms
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
                    "okx.backfill.page", symbol=symbol, interval=interval, page_rows=n
                )
            # Step the cursor backwards.
            oldest_ms = int(raw[0][0])
            if oldest_ms >= cursor_ms:
                break  # no progress
            cursor_ms = oldest_ms - step_ms
        return total

    async def backfill_trades(self, symbol: str, from_ts: datetime, to_ts: datetime) -> int:
        log.info("okx.backfill_trades.skipped", reason="stream-only policy")
        return 0

    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None:
        # Subscribe one channel per (symbol, interval).
        args = [
            {"channel": f"candle{INTERVAL_TO_BAR[i]}", "instId": _to_okx_inst_id(s)}
            for s in symbols
            for i in intervals
            if i in INTERVAL_TO_BAR
        ]
        await self._stream_args(args)

    async def stream_trades(self, symbols: list[str]) -> None:
        args = [{"channel": "trades", "instId": _to_okx_inst_id(s)} for s in symbols]
        await self._stream_args(args)

    async def _stream_args(self, args: list[dict]) -> None:
        backoff = 1.0
        sub = json.dumps({"op": "subscribe", "args": args})
        while True:
            try:
                async with websockets.connect(
                    self.WS_BASE, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("okx.ws.connected", n_args=len(args))
                    backoff = 1.0
                    await ws.send(sub)
                    async for raw in ws:
                        if raw == "pong":
                            continue
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        msg = json.loads(raw)
                        if msg.get("event") in ("subscribe", "error"):
                            if msg.get("event") == "error":
                                log.warning("okx.ws.subscribe_err", msg=msg)
                            continue
                        arg = msg.get("arg", {})
                        chan = arg.get("channel", "")
                        if chan.startswith("candle"):
                            await self._handle_candle(arg, msg)
                        elif chan == "trades":
                            await self._handle_trade(arg, msg)
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("okx.ws.disconnect", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_candle(self, arg: dict, msg: dict) -> None:
        chan = arg["channel"]
        inst = arg["instId"]
        symbol = inst.replace("-", "")
        bar = chan.replace("candle", "")
        # Map back to canonical interval names.
        interval = next((k for k, v in INTERVAL_TO_BAR.items() if v == bar), None)
        if interval is None:
            return
        rows = []
        for c in msg.get("data", []):
            # OKX confirm flag: '0' = in-progress, '1' = closed.
            if len(c) >= 9 and c[8] != "1":
                continue
            ts = datetime.fromtimestamp(int(c[0]) / 1000, tz=UTC)
            rows.append(
                (
                    self.name,
                    symbol,
                    interval,
                    ts,
                    Decimal(str(c[1])),
                    Decimal(str(c[2])),
                    Decimal(str(c[3])),
                    Decimal(str(c[4])),
                    Decimal(str(c[5])),
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

    async def _handle_trade(self, arg: dict, msg: dict) -> None:
        symbol = arg["instId"].replace("-", "")
        rows = []
        for t in msg.get("data", []):
            ts = datetime.fromtimestamp(int(t["ts"]) / 1000, tz=UTC)
            side = "buy" if t.get("side") == "buy" else "sell"
            rows.append(
                (
                    self.name,
                    symbol,
                    ts,
                    str(t["tradeId"]),
                    Decimal(str(t["px"])),
                    Decimal(str(t["sz"])),
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
