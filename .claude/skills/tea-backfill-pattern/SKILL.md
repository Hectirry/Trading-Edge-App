---
name: tea-backfill-pattern
description: >
  Idempotent backfill template for TEA TimescaleDB hypertables (Polymarket
  /prices-history, Binance OHLCV, paper settles, Coinalyze derivatives).
  Codifies the patterns from scripts/backfill_polymarket_prices_history.py
  and scripts/backfill_paper_settles.py — rate limiting, browser UA for
  Cloudflare bypass, ON CONFLICT DO NOTHING, per-token skip-done set,
  progress logging, and resume semantics. Invoke when user says "backfill
  X", "ingest historical Y", or "set up a one-shot batch ingest script".
---

A backfill is a one-shot, resumable, idempotent fill of historical
data. The user can stop and restart it without duplicates and without
re-fetching what's already done.

## Pattern at a glance

```
1. Connect to source API (HTTP) and Postgres (asyncpg).
2. Enumerate the work units (markets, days, symbols).
3. Build a "skip-done" set from the existing target table.
4. For each unit, fetch + persist with ON CONFLICT DO NOTHING.
5. Sleep between calls (rate limit).
6. Log progress every N units.
```

## File layout

```
scripts/backfill_<source>_<table>.py
infra/postgres/init/NN_<table>.sql      # schema if new
```

The schema file defines the hypertable + the conflict key:

```sql
CREATE TABLE IF NOT EXISTS market_data.<table> (
    <natural_key1> text NOT NULL,
    <natural_key2> text NOT NULL,
    ts timestamptz NOT NULL,
    <payload columns>,
    PRIMARY KEY (<natural_key1>, ts)   -- enables ON CONFLICT DO NOTHING
);
SELECT create_hypertable('market_data.<table>', 'ts',
                         chunk_time_interval => interval '1 day',
                         if_not_exists => true);
```

## Script skeleton (async, asyncpg)

```python
import argparse, asyncio, json, logging, os, time
import urllib.request, urllib.error
from datetime import UTC, datetime
import asyncpg

log = logging.getLogger("backfill.<source>")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_URL = "https://..."
SLEEP_BETWEEN_CALLS_S = 0.4   # tune to API rate limit
MIN_ROWS_PER_UNIT = 10        # threshold for "already done"

# Cloudflare 1010 bypass — Polymarket and several other providers block
# Python's default UA. Always set a real browser UA when fetching from
# upstream APIs that sit behind Cloudflare.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


def _fetch(unit_key: str, **params) -> list[dict]:
    url = f"{API_URL}?...{unit_key}..."
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return list(json.loads(resp.read()).get("history", []))
    except urllib.error.HTTPError as e:
        log.warning("api.http_err unit=%s code=%d body=%s",
                    unit_key, e.code, e.read()[:200])
        return []
    except Exception as e:
        log.warning("api.err unit=%s err=%s", unit_key, e)
        return []


async def _row_counts(conn) -> dict[str, int]:
    """Build the skip-done set by counting existing rows per natural key."""
    rows = await conn.fetch(
        "SELECT <natural_key1>, COUNT(*) AS n "
        "FROM market_data.<table> GROUP BY <natural_key1>"
    )
    return {r["<natural_key1>"]: int(r["n"]) for r in rows}


async def _persist(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO market_data.<table>
            (<natural_key1>, ts, <payload>)
        VALUES ($1, $2, $3, ...)
        ON CONFLICT (<natural_key1>, ts) DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def run(t_from: int, t_to: int) -> int:
    conn = await asyncpg.connect(dsn=_pg_dsn())
    try:
        units = await _enumerate_work(conn, t_from, t_to)
        log.info("found %d work units", len(units))
        existing = await _row_counts(conn)
        total_inserted = 0
        n_skipped = 0
        for i, unit in enumerate(units):
            key = unit["key"]
            if existing.get(key, 0) >= MIN_ROWS_PER_UNIT:
                n_skipped += 1
                continue
            rows = _fetch(key, **unit["params"])
            inserted = await _persist(conn, [...])
            total_inserted += inserted
            time.sleep(SLEEP_BETWEEN_CALLS_S)
            if (i + 1) % 25 == 0:
                log.info("progress %d / %d — rows=%d skipped=%d",
                         i + 1, len(units), total_inserted, n_skipped)
        return total_inserted
    finally:
        await conn.close()
```

## Bulk path — N rows ≥ 10k per unit

`executemany` is fine for ≤ 10k rows. For higher volume, copy into a
temp table and `INSERT ... SELECT ... ON CONFLICT DO NOTHING` from it
(direct `copy_records_to_table` bypasses the conflict clause).

## Resume + rate limiting

`MIN_ROWS_PER_UNIT` is the bar below which a unit is treated as
"partial" and re-fetched. Tune to `expected_rows × 0.6`. ON CONFLICT
DO NOTHING on the (key, ts) PK keeps re-runs idempotent.

`time.sleep(SLEEP_BETWEEN_CALLS_S)` is enough for single-process
backfills. For parallel fan-out use `aiolimiter.AsyncLimiter` with the
documented rate limit. Polymarket CLOB tolerates ~3 r/s; we use 0.4 s
(= 2.5 r/s) for headroom. Binance public is 1200 r/min.

## CLI + run

```python
ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="date_from", required=True)
ap.add_argument("--to", dest="date_to", required=True)
args = ap.parse_args()
t_from = int(datetime.fromisoformat(args.date_from).replace(tzinfo=UTC).timestamp())
t_to = int(datetime.fromisoformat(args.date_to).replace(tzinfo=UTC).timestamp())
asyncio.run(run(t_from, t_to))
```

```bash
docker compose exec tea-engine python scripts/backfill_<source>_<table>.py \
    --from 2026-01-01 --to 2026-04-25
```

## Reference scripts

- `scripts/backfill_polymarket_prices_history.py` — Cloudflare UA,
  per-token skip-done, fidelity=1 window endpoint.
- `scripts/backfill_paper_settles.py` — settle from `crypto_ohlcv` 1m.

## Don't

- Don't UPDATE on conflict. Reconciliation is a separate script.
- Don't `asyncio.gather` 1000 fetches without a limiter — IP ban.
- Don't omit progress logs. A 6-h silent run looks hung.
- Don't hardcode the DSN. Use `_pg_dsn()`.
