"""One-off backfill for paper orders whose settle fill never landed.

Reason: between engine deploys, the recorder sometimes unsubscribes from
a market at ``closed=true`` before the driver sees a tick with
``t_in_window >= 300``, so ``_settle_position`` never fires and the
order rows sit forever with only an entry fill. (Fixed going forward by
the ``_settle_watchdog_loop``.)

Usage: ``docker compose exec tea-engine python scripts/backfill_paper_settles.py``
Idempotent: inserts use ON CONFLICT DO NOTHING on (fill_id, ts).

Performance: 3 round-trips total regardless of N orders.
1. Single SELECT joining ``trading.orders`` → ``polymarket_markets`` on
   slug derived in SQL via regexp_replace (matches the in-Python
   ``_slug_from_instrument``). Brings ``qty`` along so we don't re-query.
2. Bulk OHLCV 1m closes for the union of {close_minute, open_minute}
   across all orders.
3. Bulk paper_ticks open_prices via ``DISTINCT ON (condition_id)`` for
   the union of condition_ids.
Replaces a prior 4-N-queries-per-order loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
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
    # Strip "-YES.POLYMARKET" or "-NO.POLYMARKET". Mirrored in SQL via
    # `regexp_replace(..., '-(YES|NO)\.POLYMARKET$', '')` in
    # `_load_pending_orders`; keep them in sync.
    return instrument_id.rsplit(".", 1)[0].rsplit("-", 1)[0]


async def _load_pending_orders(conn) -> list[dict]:
    """Orders missing a settle fill, joined to their polymarket market.

    Slug is derived in SQL via regexp so we don't pay an N+1 to look up
    `polymarket_markets.condition_id`/`close_time` per order. Orders
    without a matching market row come back with NULL columns and are
    filtered downstream by reason bucket.
    """
    rows = await conn.fetch(
        r"""
        WITH pending AS (
            SELECT
                o.order_id, o.instrument_id, o.qty,
                o.price AS entry_price, o.ts_submit,
                regexp_replace(o.instrument_id, '-(YES|NO)\.POLYMARKET$', '') AS slug
            FROM trading.orders o
            WHERE o.mode = 'paper'
              AND NOT EXISTS (
                  SELECT 1 FROM trading.fills f
                  WHERE f.order_id = o.order_id
                    AND f.metadata::jsonb->>'kind' = 'settle'
              )
        )
        SELECT p.order_id, p.instrument_id, p.qty, p.entry_price, p.ts_submit,
               p.slug, pm.condition_id, pm.close_time
        FROM pending p
        LEFT JOIN market_data.polymarket_markets pm ON pm.slug = p.slug
        ORDER BY p.ts_submit ASC
        """
    )
    return [dict(r) for r in rows]


async def _bulk_ohlcv_closes(conn, minutes: set[datetime]) -> dict[datetime, float]:
    """Map each minute_dt → Binance 1m close at that minute.

    `minutes` must already be minute-floored. Missing/NULL closes are
    omitted; the caller decides the fallback.
    """
    if not minutes:
        return {}
    rows = await conn.fetch(
        "SELECT ts, close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' "
        "AND ts = ANY($1::timestamptz[])",
        list(minutes),
    )
    return {r["ts"]: float(r["close"]) for r in rows if r["close"] is not None}


async def _bulk_paper_open_prices(conn, condition_ids: set[str]) -> dict[str, float]:
    """First positive open_price per condition_id, recorded in paper_ticks.

    Mirrors the prior per-order ``ORDER BY ts ASC LIMIT 1`` via
    ``DISTINCT ON`` so one round-trip covers all condition_ids.
    """
    if not condition_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (condition_id) condition_id, open_price
        FROM market_data.paper_ticks
        WHERE condition_id = ANY($1::text[])
          AND open_price IS NOT NULL AND open_price > 0
        ORDER BY condition_id, ts ASC
        """,
        list(condition_ids),
    )
    return {r["condition_id"]: float(r["open_price"]) for r in rows}


def _minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


async def run() -> None:
    async with acquire() as conn:
        orders = await _load_pending_orders(conn)
        log.info("found %d paper orders missing settle fills", len(orders))
        if not orders:
            await close_pool()
            return

        now = datetime.now(tz=UTC)
        # Compute the union of OHLCV minutes + condition_ids we'll need
        # so the two bulk fetches return a complete superset.
        close_minutes: set[datetime] = set()
        open_minutes: set[datetime] = set()
        condition_ids: set[str] = set()
        for o in orders:
            close_dt = o.get("close_time")
            if not close_dt or close_dt > now:
                continue
            close_minutes.add(_minute_floor(close_dt))
            open_minutes.add(_minute_floor(close_dt - timedelta(minutes=5)))
            if o.get("condition_id"):
                condition_ids.add(o["condition_id"])

        ohlcv_closes = await _bulk_ohlcv_closes(conn, close_minutes | open_minutes)
        paper_opens = await _bulk_paper_open_prices(conn, condition_ids)

        fill_rows: list[tuple] = []
        skip_reasons: dict[str, int] = {}
        for o in orders:
            close_dt = o.get("close_time")
            if not close_dt:
                skip_reasons["no_market_row"] = skip_reasons.get("no_market_row", 0) + 1
                continue
            if close_dt > now:
                skip_reasons["still_open"] = skip_reasons.get("still_open", 0) + 1
                continue
            settle_price = ohlcv_closes.get(_minute_floor(close_dt))
            if settle_price is None:
                skip_reasons["no_ohlcv_close"] = skip_reasons.get("no_ohlcv_close", 0) + 1
                continue
            open_price = paper_opens.get(o["condition_id"]) or ohlcv_closes.get(
                _minute_floor(close_dt - timedelta(minutes=5))
            )
            if not open_price or open_price <= 0:
                skip_reasons["no_open_price"] = skip_reasons.get("no_open_price", 0) + 1
                continue
            went_up = settle_price > open_price
            entry_price = float(o["entry_price"])
            qty = float(o["qty"])
            stake_usd = qty * entry_price
            side = _side_from_slug(o["instrument_id"])
            resolution, exit_price, pnl = settle_math(
                side=side,
                entry_price=entry_price,
                stake_usd=stake_usd,
                fee=0.0,
                outcome_went_up=went_up,
            )
            fill_rows.append(
                (
                    f"{o['order_id']}-exit",
                    o["order_id"],
                    close_dt,
                    Decimal(str(exit_price)),
                    Decimal(str(qty)),
                    json.dumps(
                        {
                            "kind": "settle",
                            "resolution": resolution,
                            "pnl": round(pnl, 6),
                            "source": "backfill_ohlcv",
                        }
                    ),
                )
            )
            log.info(
                "settled %s open=%.2f close=%.2f went_up=%s resolution=%s pnl=%.4f",
                o["slug"],
                open_price,
                settle_price,
                went_up,
                resolution,
                pnl,
            )

        if fill_rows:
            await conn.executemany(
                """
                INSERT INTO trading.fills
                    (fill_id, order_id, ts, price, qty, liquidity_side, fee,
                     fee_currency, mode, backtest_id, metadata)
                VALUES ($1, $2, $3, $4, $5, 'TAKER', 0, 'USDC', 'paper', NULL, $6::jsonb)
                ON CONFLICT (fill_id, ts) DO NOTHING
                """,
                fill_rows,
            )

        log.info(
            "done: settled=%d skipped=%d total=%d skip_reasons=%s",
            len(fill_rows),
            sum(skip_reasons.values()),
            len(orders),
            skip_reasons,
        )
    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())
