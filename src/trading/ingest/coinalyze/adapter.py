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
        self, *, since_ts: int, until_ts: int,
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
                "coinalyze.non_200", status=r.status_code, body=r.text[:200],
            )
            return None
        try:
            return r.json()
        except Exception:
            return None


def _expand_bars_to_clusters(
    bars_payload: list[dict] | None,
    current_spot: float | None,
) -> list[tuple]:
    """Collapse the Coinalyze response shape into
    ``(ts_dt, symbol, side, price, size_usd, source)`` tuples ready for
    ``upsert_many``. We split each bar's long+short liquidation amount
    into two cluster rows at the bar's midpoint price. If the current
    spot is unknown we use the bar's price proxy (when Coinalyze
    provides one) else skip.
    """
    from datetime import UTC, datetime

    out: list[tuple] = []
    if not bars_payload:
        return out
    for symbol_entry in bars_payload:
        symbol = symbol_entry.get("symbol") or "BTCUSDT_PERP.A"
        history = symbol_entry.get("history") or []
        for bar in history:
            ts = bar.get("t")
            if ts is None:
                continue
            long_amt = float(bar.get("l") or 0.0)   # long liquidations $
            short_amt = float(bar.get("s") or 0.0)  # short liquidations $
            price_proxy = bar.get("p") or current_spot
            if price_proxy is None:
                continue
            ts_dt = datetime.fromtimestamp(int(ts), tz=UTC)
            if long_amt > 0:
                out.append((
                    ts_dt, symbol, "long",
                    Decimal(str(price_proxy)),
                    Decimal(str(long_amt)),
                    "coinalyze",
                ))
            if short_amt > 0:
                out.append((
                    ts_dt, symbol, "short",
                    Decimal(str(price_proxy)),
                    Decimal(str(short_amt)),
                    "coinalyze",
                ))
    return out


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
            payload = await client.liquidation_history(
                since_ts=now_ts - 300, until_ts=now_ts,
            )
            rows = _expand_bars_to_clusters(payload, current_spot=None)
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
