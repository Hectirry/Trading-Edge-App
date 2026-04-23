"""One-off backfill for paper orders whose settle fill never landed.

Reason: between engine deploys, the recorder sometimes unsubscribes from
a market at ``closed=true`` before the driver sees a tick with
``t_in_window >= 300``, so ``_settle_position`` never fires and the
order rows sit forever with only an entry fill. (Fixed going forward by
the ``_settle_watchdog_loop``.)

Usage: ``docker compose exec tea-engine python scripts/backfill_paper_settles.py``
Idempotent: inserts use ON CONFLICT DO NOTHING on (fill_id, ts).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from trading.common.db import acquire, close_pool
from trading.engine.fill_model import settle as settle_math
from trading.engine.types import Side

log = logging.getLogger("backfill.paper_settles")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _side_from_slug(instrument_id: str) -> Side:
    # instrument_id: "btc-updown-5m-<TS>-YES.POLYMARKET" or "...-NO.POLYMARKET"
    return Side.YES_UP if "-YES." in instrument_id else Side.YES_DOWN


def _slug_from_instrument(instrument_id: str) -> str:
    # Strip "-YES.POLYMARKET" or "-NO.POLYMARKET"
    return instrument_id.rsplit(".", 1)[0].rsplit("-", 1)[0]


async def _settle_price_at(conn, close_dt: datetime) -> float | None:
    minute_dt = close_dt.replace(second=0, microsecond=0)
    row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' AND ts=$1",
        minute_dt,
    )
    if row is None:
        return None
    return float(row["close"])


async def _open_price_for(conn, condition_id: str, close_dt: datetime) -> float | None:
    # paper_ticks sometimes stores open_price=0 (recorder didn't capture
    # the open) so we don't trust it when it's ≤ 0; fall back to the
    # Binance 1 m candle ending at window_open = close - 5 min.
    row = await conn.fetchrow(
        "SELECT open_price FROM market_data.paper_ticks "
        "WHERE condition_id = $1 AND open_price IS NOT NULL AND open_price > 0 "
        "ORDER BY ts ASC LIMIT 1",
        condition_id,
    )
    if row is not None and row["open_price"] is not None:
        return float(row["open_price"])
    from datetime import timedelta
    open_minute = (close_dt - timedelta(minutes=5)).replace(second=0, microsecond=0)
    row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' AND ts=$1",
        open_minute,
    )
    if row is None:
        return None
    return float(row["close"])


async def run() -> None:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.order_id, o.instrument_id, o.price AS entry_price,
                   o.ts_submit
            FROM trading.orders o
            WHERE o.mode = 'paper'
              AND NOT EXISTS (
                  SELECT 1 FROM trading.fills f
                  WHERE f.order_id = o.order_id
                    AND f.metadata::jsonb->>'kind' = 'settle'
              )
            ORDER BY o.ts_submit ASC
            """
        )
        log.info("found %d paper orders missing settle fills", len(rows))
        settled = 0
        skipped = 0
        for r in rows:
            instrument = r["instrument_id"]
            slug = _slug_from_instrument(instrument)
            mkt = await conn.fetchrow(
                "SELECT condition_id, close_time FROM market_data.polymarket_markets "
                "WHERE slug = $1",
                slug,
            )
            if mkt is None or mkt["close_time"] is None:
                log.warning("skip %s: market row missing close_time", slug)
                skipped += 1
                continue
            close_dt = mkt["close_time"]
            if close_dt > datetime.now(tz=UTC):
                log.info("skip %s: still open (close=%s)", slug, close_dt)
                skipped += 1
                continue
            settle_price = await _settle_price_at(conn, close_dt)
            if settle_price is None:
                log.warning("skip %s: no ohlcv close at %s", slug, close_dt)
                skipped += 1
                continue
            open_price = await _open_price_for(conn, mkt["condition_id"], close_dt)
            if open_price is None or open_price <= 0:
                log.warning("skip %s: no open_price in paper_ticks", slug)
                skipped += 1
                continue
            went_up = settle_price > open_price
            # Reverse-engineer stake_usd from order.price * qty — paper_driver
            # sets qty = stake / entry_price, so stake = qty * entry_price.
            order_qty_row = await conn.fetchrow(
                "SELECT qty FROM trading.orders WHERE order_id = $1",
                r["order_id"],
            )
            qty = float(order_qty_row["qty"])
            entry_price = float(r["entry_price"])
            stake_usd = qty * entry_price
            side = _side_from_slug(instrument)
            resolution, exit_price, pnl = settle_math(
                side=side,
                entry_price=entry_price,
                stake_usd=stake_usd,
                fee=0.0,
                outcome_went_up=went_up,
            )
            fill_id = f"{r['order_id']}-exit"
            await conn.execute(
                """
                INSERT INTO trading.fills
                    (fill_id, order_id, ts, price, qty, liquidity_side, fee,
                     fee_currency, mode, backtest_id, metadata)
                VALUES ($1, $2, $3, $4, $5, 'TAKER', 0, 'USDC', 'paper', NULL, $6::jsonb)
                ON CONFLICT (fill_id, ts) DO NOTHING
                """,
                fill_id,
                r["order_id"],
                close_dt,
                Decimal(str(exit_price)),
                Decimal(str(qty)),
                json.dumps({
                    "kind": "settle",
                    "resolution": resolution,
                    "pnl": round(pnl, 6),
                    "source": "backfill_ohlcv",
                }),
            )
            log.info(
                "settled %s open=%.2f close=%.2f went_up=%s resolution=%s pnl=%.4f",
                slug, open_price, settle_price, went_up, resolution, pnl,
            )
            settled += 1
        log.info("done: settled=%d skipped=%d total=%d", settled, skipped, len(rows))
    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())
