"""One-shot backfill of market_data.liquidation_clusters from
Coinalyze /liquidation-history. Coinalyze keeps history for the
free tier; we pull in 1-day chunks to stay well under rate limits.

Usage:
  python scripts/backfill_coinalyze.py --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import httpx


async def _load_btc_closes(conn, since: datetime, until: datetime) -> dict[int, float]:
    """Bulk-fetch Binance BTCUSDT 1m closes in [since, until], indexed by
    unix-second-of-minute. One round-trip replaces a per-bar lookup that
    used to open a fresh asyncpg connection per liquidation bar."""
    rows = await conn.fetch(
        "SELECT EXTRACT(EPOCH FROM ts)::bigint AS t, close "
        "FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' "
        "AND ts BETWEEN $1 AND $2",
        since,
        until,
    )
    return {int(r["t"]): float(r["close"]) for r in rows if r["close"] is not None}


def _spot_for_bar(closes: dict[int, float], bar_ts: int) -> float | None:
    """Closest 1m close at or before `bar_ts` (unix s). Walks back up to
    5 minutes; returns None if the OHLCV table has no candle in range."""
    minute = (bar_ts // 60) * 60
    for offset in range(0, 6):
        v = closes.get(minute - offset * 60)
        if v is not None:
            return v
    return None


async def fetch_chunk(
    client: httpx.AsyncClient, api_key: str, since_ts: int, until_ts: int
) -> list[dict]:
    r = await client.get(
        "https://api.coinalyze.net/v1/liquidation-history",
        params={
            "symbols": "BTCUSDT_PERP.A",
            "interval": "1min",
            "from": since_ts,
            "to": until_ts,
            "api_key": api_key,
        },
        timeout=20.0,
    )
    if r.status_code != 200:
        print(f"  WARN status={r.status_code}: {r.text[:120]}", file=sys.stderr)
        return []
    return r.json() or []


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--api-key", default=os.environ.get("COINALYZE_API_KEY"))
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("DATABASE_URL not set")
    if not args.api_key:
        raise SystemExit("COINALYZE_API_KEY not set")

    until = datetime.now(tz=UTC).replace(microsecond=0)
    since = until - timedelta(days=args.days)
    print(f"backfill window: {since.isoformat()} → {until.isoformat()}")

    rows_total = 0
    async with httpx.AsyncClient() as client:
        cursor = since
        while cursor < until:
            chunk_end = min(cursor + timedelta(hours=24), until)
            payload = await fetch_chunk(
                client,
                args.api_key,
                int(cursor.timestamp()),
                int(chunk_end.timestamp()),
            )
            if not payload:
                cursor = chunk_end
                continue
            # One asyncpg connection per chunk: preload the OHLCV closes
            # that cover this chunk window (with a 5-min lookback so the
            # nearest-≤ lookup never misses) and reuse the same connection
            # for the upsert.
            conn = await asyncpg.connect(dsn=args.dsn)
            try:
                closes = await _load_btc_closes(conn, cursor - timedelta(minutes=5), chunk_end)
                tuples: list[tuple] = []
                for sym_entry in payload:
                    symbol = sym_entry.get("symbol") or "BTCUSDT_PERP.A"
                    history = sym_entry.get("history") or []
                    for bar in history:
                        ts = bar.get("t")
                        if ts is None:
                            continue
                        long_amt = float(bar.get("l") or 0.0)
                        short_amt = float(bar.get("s") or 0.0)
                        if long_amt <= 0 and short_amt <= 0:
                            continue
                        spot = _spot_for_bar(closes, int(ts))
                        if spot is None:
                            continue
                        ts_dt = datetime.fromtimestamp(int(ts), tz=UTC)
                        if long_amt > 0:
                            tuples.append(
                                (
                                    ts_dt,
                                    symbol,
                                    "long",
                                    Decimal(str(spot)),
                                    Decimal(str(long_amt)),
                                    "coinalyze",
                                )
                            )
                        if short_amt > 0:
                            tuples.append(
                                (
                                    ts_dt,
                                    symbol,
                                    "short",
                                    Decimal(str(spot)),
                                    Decimal(str(short_amt)),
                                    "coinalyze",
                                )
                            )
                if tuples:
                    await conn.executemany(
                        """
                        INSERT INTO market_data.liquidation_clusters
                          (ts, symbol, side, price, size_usd, source)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (symbol, side, price, ts) DO UPDATE
                        SET size_usd = EXCLUDED.size_usd
                        """,
                        tuples,
                    )
            finally:
                await conn.close()
            rows_total += len(tuples)
            print(f"  {cursor.date()} → {chunk_end.date()}: +{len(tuples)} rows")
            cursor = chunk_end
    print(f"DONE total inserted: {rows_total}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
