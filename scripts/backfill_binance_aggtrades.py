"""Backfill `market_data.crypto_trades` for Binance BTCUSDT in the
microstructure windows [open + 120, open + 210] of every BTC up/down
5 m market in a date range. This is the window v3 strategy reads at
decision time (`as_of = open + 210`, microstructure_window_s = 90).

Why not continuous? 90 d of continuous BTCUSDT aggTrades is ~30 GB +
~50 M rows; sampled per-market is ~12 M rows for the same training
universe. 100x reduction; same training signal. The continuous WS
ingest in `tea-ingestor` covers live state.

Source: https://api.binance.com/api/v3/aggTrades — public, no auth,
weight=1, 1200 req/min limit per IP. We rate-limit to 1000 r/min for
headroom.

Idempotent: ON CONFLICT (exchange, symbol, trade_id, ts) DO NOTHING
on the existing PK. Resume semantics: skip a market if its window is
already ≥80 % covered.

Usage:
    docker compose exec tea-engine python scripts/backfill_binance_aggtrades.py \\
        --from 2026-01-25 --to 2026-04-25
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
from aiolimiter import AsyncLimiter

log = logging.getLogger("backfill.binance.aggtrades")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_URL = "https://api.binance.com/api/v3/aggTrades"
SYMBOL = "BTCUSDT"
EXCHANGE = "binance"
WINDOW_OFFSET_START_S = 120  # as_of - 90 = open + 120
WINDOW_OFFSET_END_S = 210    # as_of = open + 210
LIMIT = 1000
RATE_LIMIT_PER_MIN = 1000
MIN_COVERAGE_FRACTION = 0.8  # skip windows already this covered
EXPECTED_TRADES_PER_WINDOW = 1000  # ~11 trades/s × 90 s, conservative


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


async def _markets_in_range(conn, t_from: int, t_to: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT slug,
               EXTRACT(EPOCH FROM open_time)::bigint AS open_unix,
               EXTRACT(EPOCH FROM close_time)::bigint AS close_unix
        FROM market_data.polymarket_markets
        WHERE slug LIKE 'btc-%updown-5m-%'
          AND close_time >= to_timestamp($1)
          AND close_time <= to_timestamp($2)
        ORDER BY open_time
        """,
        t_from,
        t_to,
    )
    return [
        {
            "slug": r["slug"],
            "open_unix": int(r["open_unix"]),
            "close_unix": int(r["close_unix"]),
            "win_start": int(r["open_unix"]) + WINDOW_OFFSET_START_S,
            "win_end": int(r["open_unix"]) + WINDOW_OFFSET_END_S,
        }
        for r in rows
    ]


async def _coverage_for_windows(
    conn, markets: list[dict[str, Any]]
) -> dict[str, int]:
    """Return slug → trade count already in [win_start, win_end]. Used to
    skip markets that are already sufficiently covered."""
    if not markets:
        return {}
    win_starts = [m["win_start"] for m in markets]
    win_ends = [m["win_end"] for m in markets]
    slugs = [m["slug"] for m in markets]
    rows = await conn.fetch(
        """
        WITH inputs AS (
            SELECT * FROM unnest($1::text[], $2::bigint[], $3::bigint[]) AS t(slug, ws, we)
        )
        SELECT i.slug, COUNT(t.ts) AS n
        FROM inputs i
        LEFT JOIN market_data.crypto_trades t
          ON t.exchange = $4 AND t.symbol = $5
         AND t.ts >= to_timestamp(i.ws)
         AND t.ts <= to_timestamp(i.we)
        GROUP BY i.slug
        """,
        slugs,
        win_starts,
        win_ends,
        EXCHANGE,
        SYMBOL,
    )
    return {r["slug"]: int(r["n"]) for r in rows}


async def _fetch_window(
    client: httpx.AsyncClient, limiter: AsyncLimiter, win_start: int, win_end: int
) -> list[dict[str, Any]]:
    """Fetch all aggTrades in [win_start, win_end] in unix seconds. Paginates
    via fromId when the 1000-row limit is hit (Binance returns oldest 1000
    in window; we step forward by trade_id+1)."""
    out: list[dict[str, Any]] = []
    start_ms = win_start * 1000
    end_ms = win_end * 1000
    next_from_id: int | None = None
    while True:
        async with limiter:
            params: dict[str, Any] = {"symbol": SYMBOL, "limit": LIMIT}
            if next_from_id is not None:
                params["fromId"] = next_from_id
            else:
                params["startTime"] = start_ms
                params["endTime"] = end_ms
            try:
                r = await client.get(API_URL, params=params, timeout=15.0)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("api.err win=[%d,%d] err=%s", win_start, win_end, e)
                return out
            batch = r.json()
        if not batch:
            break
        # When using fromId we get trades regardless of time — clip to window.
        if next_from_id is not None:
            batch = [b for b in batch if start_ms <= int(b["T"]) <= end_ms]
            if not batch:
                break
        out.extend(batch)
        if len(r.json()) < LIMIT:
            break
        # Step past the last trade by id to fetch the next page.
        next_from_id = int(batch[-1]["a"]) + 1
        # Defensive: stop if the last trade is already at/past end_ms.
        if int(batch[-1]["T"]) >= end_ms:
            break
    return out


def _to_row(a: dict[str, Any]) -> tuple:
    return (
        EXCHANGE,
        SYMBOL,
        datetime.fromtimestamp(int(a["T"]) / 1000, tz=UTC),
        str(a["a"]),
        Decimal(a["p"]),
        Decimal(a["q"]),
        "sell" if a["m"] else "buy",
    )


async def _persist(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO market_data.crypto_trades
            (exchange, symbol, ts, trade_id, price, qty, side)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (exchange, symbol, trade_id, ts) DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def run(t_from: int, t_to: int) -> dict[str, int]:
    started = datetime.now(tz=UTC)
    conn = await asyncpg.connect(dsn=_pg_dsn())
    try:
        markets = await _markets_in_range(conn, t_from, t_to)
        log.info("markets in window: %d", len(markets))
        if not markets:
            return {"markets": 0, "inserted": 0, "skipped": 0}
        coverage = await _coverage_for_windows(conn, markets)
        threshold = int(EXPECTED_TRADES_PER_WINDOW * MIN_COVERAGE_FRACTION)
        n_skipped = sum(1 for m in markets if coverage.get(m["slug"], 0) >= threshold)
        log.info("already-covered (≥%d trades): %d / %d", threshold, n_skipped, len(markets))

        limiter = AsyncLimiter(RATE_LIMIT_PER_MIN, time_period=60)
        total_inserted = 0
        n_processed = 0
        async with httpx.AsyncClient(http2=False) as client:
            for i, m in enumerate(markets):
                if coverage.get(m["slug"], 0) >= threshold:
                    continue
                trades = await _fetch_window(client, limiter, m["win_start"], m["win_end"])
                rows = [_to_row(t) for t in trades]
                inserted = await _persist(conn, rows)
                total_inserted += inserted
                n_processed += 1
                if (i + 1) % 100 == 0:
                    elapsed = (datetime.now(tz=UTC) - started).total_seconds()
                    log.info(
                        "progress %d / %d markets — processed=%d inserted=%d elapsed=%.1fs",
                        i + 1,
                        len(markets),
                        n_processed,
                        total_inserted,
                        elapsed,
                    )

        elapsed = (datetime.now(tz=UTC) - started).total_seconds()
        log.info(
            "done: markets=%d processed=%d skipped=%d inserted=%d elapsed=%.1fs",
            len(markets),
            n_processed,
            n_skipped,
            total_inserted,
            elapsed,
        )
        return {
            "markets": len(markets),
            "processed": n_processed,
            "skipped": n_skipped,
            "inserted": total_inserted,
            "elapsed_s": int(elapsed),
        }
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    args = ap.parse_args()
    t_from = int(datetime.fromisoformat(args.date_from).replace(tzinfo=UTC).timestamp())
    t_to = int(datetime.fromisoformat(args.date_to).replace(tzinfo=UTC).timestamp())
    asyncio.run(run(t_from, t_to))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
