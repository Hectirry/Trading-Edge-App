"""Backfill `market_data.polymarket_prices_history` from the Polymarket
/prices-history endpoint.

For each resolved BTC up/down 5 m market in the training set range,
fetch the per-token (YES + NO) price history, persist with a timezone-
aware `ts` column. Idempotent via PRIMARY KEY (token_id, ts).

API note: the public CLOB endpoint requires either an `interval` (with
a minimum `fidelity` per range) OR an explicit `startTs`/`endTs` pair
plus `fidelity`. We use startTs/endTs scoped to the market's
[open - 5min, close + 5min] window with `fidelity=1` (1 sample per
~minute), which is enough to read the price at the strategy as_of
(open + 210 s) without spamming the API.

Rate limiting: a 0.4 s sleep between calls. The endpoint is generous
(~3 r/s sustained per IP per casual observation) but we don't need
speed — once-off backfill, can run for hours.

Usage:
    docker compose exec tea-engine python scripts/backfill_polymarket_prices_history.py \\
        --from 2026-04-22 --to 2026-04-25
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg
import urllib.request
import urllib.error

log = logging.getLogger("backfill.polymarket_prices_history")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_URL = "https://clob.polymarket.com/prices-history"
# Polymarket's CLOB demands a minimum fidelity-per-range. fidelity=1 is the
# tightest 1-min granularity it accepts for windows ≤ 1 day.
DEFAULT_FIDELITY = 1
SLEEP_BETWEEN_CALLS_S = 0.4


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _fetch_history(token_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    url = (
        f"{API_URL}?market={token_id}"
        f"&startTs={start_ts}&endTs={end_ts}&fidelity={DEFAULT_FIDELITY}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        log.warning("api.http_err token=%s code=%d body=%s", token_id, e.code, e.read()[:200])
        return []
    except Exception as e:
        log.warning("api.err token=%s err=%s", token_id, e)
        return []
    try:
        payload = json.loads(body)
    except Exception as e:
        log.warning("api.json_err token=%s err=%s body=%s", token_id, e, body[:200])
        return []
    return list(payload.get("history", []))


async def _markets_in_range(conn, t_from: int, t_to: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT condition_id, slug, metadata->>'clobTokenIds' AS token_ids_json,
               EXTRACT(EPOCH FROM open_time)::bigint AS open_unix,
               EXTRACT(EPOCH FROM close_time)::bigint AS close_unix
        FROM market_data.polymarket_markets
        WHERE slug LIKE 'btc-%updown-5m-%'
          AND resolved = true
          AND close_time >= to_timestamp($1)
          AND close_time <= to_timestamp($2)
        ORDER BY close_time
        """,
        t_from,
        t_to,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r["token_ids_json"]:
            continue
        try:
            token_ids = json.loads(r["token_ids_json"])
        except Exception:
            continue
        if not isinstance(token_ids, list) or len(token_ids) < 2:
            continue
        out.append(
            {
                "condition_id": r["condition_id"],
                "slug": r["slug"],
                "open_unix": r["open_unix"],
                "close_unix": r["close_unix"],
                "yes_token": str(token_ids[0]),
                "no_token": str(token_ids[1]),
            }
        )
    return out


async def _persist(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO market_data.polymarket_prices_history
            (condition_id, token_id, outcome, ts, price)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (token_id, ts) DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def run(t_from: int, t_to: int) -> int:
    pg_dsn = _pg_dsn()
    conn = await asyncpg.connect(dsn=pg_dsn)
    try:
        markets = await _markets_in_range(conn, t_from, t_to)
        log.info("found %d resolved BTC up/down 5m markets in window", len(markets))
        if not markets:
            return 0

        total_inserted = 0
        for i, m in enumerate(markets):
            # Window: [open - 5min, close + 5min] in unix seconds.
            start_ts = max(0, int(m["open_unix"]) - 300)
            end_ts = int(m["close_unix"]) + 300
            for outcome, token in (("YES", m["yes_token"]), ("NO", m["no_token"])):
                history = _fetch_history(token, start_ts, end_ts)
                rows = [
                    (
                        m["condition_id"],
                        token,
                        outcome,
                        datetime.fromtimestamp(int(h["t"]), tz=UTC),
                        float(h["p"]),
                    )
                    for h in history
                    if "t" in h and "p" in h
                ]
                inserted = await _persist(conn, rows)
                total_inserted += inserted
                time.sleep(SLEEP_BETWEEN_CALLS_S)
            if (i + 1) % 25 == 0:
                log.info(
                    "progress %d / %d markets — total rows persisted: %d",
                    i + 1,
                    len(markets),
                    total_inserted,
                )

        log.info("done: persisted %d rows for %d markets", total_inserted, len(markets))
        return total_inserted
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
