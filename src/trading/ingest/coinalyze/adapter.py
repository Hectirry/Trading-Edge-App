"""Coinalyze liquidation-history ingestor (ADR 0012).

Polls ``/liquidation-history`` every 60 s, collapses the returned
bars into cluster rows (``long`` / ``short`` sides with a price and
a size_usd), and upserts into ``market_data.liquidation_clusters``.
Free tier rate limit (40 req/min) is respected by the asyncio loop
cadence — nothing stronger needed at 1 req/min.

Graceful degradation: a 401/403/5xx just logs + skips the window so
the strategy's ``liquidation_gravity_score`` falls back to zero and
the v1-B confidence cap kicks in (ADR 0012, consequences).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from trading.common.config import get_settings
from trading.common.db import upsert_many
from trading.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class CoinalyzeClient:
    symbols: str = "BTCUSDT_PERP.A"
    interval: str = "1min"

    async def liquidation_history(
        self,
        *,
        since_ts: int,
        until_ts: int,
    ) -> list[dict] | None:
        settings = get_settings()
        if not settings.coinalyze_api_key:
            return None
        url = f"{settings.coinalyze_base_url}/liquidation-history"
        params = {
            "symbols": self.symbols,
            "interval": self.interval,
            "from": since_ts,
            "to": until_ts,
            "api_key": settings.coinalyze_api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
        except Exception as e:
            log.warning("coinalyze.fetch_err", err=str(e))
            return None
        if r.status_code != 200:
            log.warning(
                "coinalyze.non_200",
                status=r.status_code,
                body=r.text[:200],
            )
            return None
        try:
            return r.json()
        except Exception:
            return None


def _spot_for_bar(closes_by_minute: dict[int, float], bar_ts: int) -> float | None:
    """Closest 1m close at or before `bar_ts` (unix s). Walks back up to
    5 minutes; returns None if the OHLCV map has no candle in range.
    Mirrors `scripts/backfill_coinalyze._spot_for_bar` so live + backfill
    anchor liquidation clusters at the same price reference."""
    minute = (bar_ts // 60) * 60
    for offset in range(0, 6):
        v = closes_by_minute.get(minute - offset * 60)
        if v is not None:
            return v
    return None


def _expand_bars_to_clusters(
    bars_payload: list[dict] | None,
    closes_by_minute: dict[int, float] | None,
) -> list[tuple]:
    """Collapse the Coinalyze response shape into
    ``(ts_dt, symbol, side, price, size_usd, source)`` tuples ready for
    ``upsert_many``. We split each bar's long+short liquidation amount
    into two cluster rows at the bar's midpoint price. Each bar resolves
    its price independently: the bar's own ``p`` field if present, else
    the Binance 1m close at the bar's minute, else skip. Falling back to
    a single ``current_spot`` for every bar would collapse every cluster
    in the polling window to the same price and distort
    liquidation_gravity.
    """
    from datetime import UTC, datetime

    out: list[tuple] = []
    if not bars_payload:
        return out
    closes = closes_by_minute or {}
    for symbol_entry in bars_payload:
        symbol = symbol_entry.get("symbol") or "BTCUSDT_PERP.A"
        history = symbol_entry.get("history") or []
        for bar in history:
            ts = bar.get("t")
            if ts is None:
                continue
            long_amt = float(bar.get("l") or 0.0)  # long liquidations $
            short_amt = float(bar.get("s") or 0.0)  # short liquidations $
            price_proxy = bar.get("p") or _spot_for_bar(closes, int(ts))
            if price_proxy is None:
                continue
            ts_dt = datetime.fromtimestamp(int(ts), tz=UTC)
            if long_amt > 0:
                out.append(
                    (
                        ts_dt,
                        symbol,
                        "long",
                        Decimal(str(price_proxy)),
                        Decimal(str(long_amt)),
                        "coinalyze",
                    )
                )
            if short_amt > 0:
                out.append(
                    (
                        ts_dt,
                        symbol,
                        "short",
                        Decimal(str(price_proxy)),
                        Decimal(str(short_amt)),
                        "coinalyze",
                    )
                )
    return out


async def _btc_closes_for_window(since_ts: int, until_ts: int) -> dict[int, float]:
    """Bulk-fetch Binance BTCUSDT 1m closes for the polling window,
    indexed by unix-second-of-minute. One round-trip replaces the
    "latest spot" single-value lookup, so each liquidation bar can be
    anchored at its own minute's close instead of all sharing the same
    reference.
    """
    from trading.common.db import acquire

    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                "SELECT EXTRACT(EPOCH FROM ts)::bigint AS t, close "
                "FROM market_data.crypto_ohlcv "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' "
                "AND ts BETWEEN to_timestamp($1) AND to_timestamp($2)",
                since_ts - 300,  # 5-min lookback for nearest-≤ fallback
                until_ts,
            )
    except Exception:
        return {}
    return {int(r["t"]): float(r["close"]) for r in rows if r["close"] is not None}


async def run_liquidation_loop() -> None:
    settings = get_settings()
    if not settings.coinalyze_api_key:
        log.warning("coinalyze.disabled_no_api_key")
        return
    client = CoinalyzeClient()
    interval_s = int(settings.coinalyze_poll_interval_s)
    log.info("coinalyze.loop.started", interval_s=interval_s)
    while True:
        try:
            now_ts = int(time.time())
            since_ts = now_ts - 300
            payload = await client.liquidation_history(
                since_ts=since_ts,
                until_ts=now_ts,
            )
            closes = await _btc_closes_for_window(since_ts, now_ts)
            rows = _expand_bars_to_clusters(payload, closes_by_minute=closes)
            if rows:
                await upsert_many(
                    "market_data.liquidation_clusters",
                    ["ts", "symbol", "side", "price", "size_usd", "source"],
                    rows,
                    ["symbol", "side", "price", "ts"],
                )
                log.info("coinalyze.upsert", n=len(rows))
        except Exception as e:
            log.exception("coinalyze.loop.err", err=str(e))
        await asyncio.sleep(interval_s)
