from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from trading.common.config import get_settings
from trading.common.logging import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            settings = get_settings()
            _pool = await asyncpg.create_pool(
                dsn=settings.pg_dsn,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            log.info("db.pool.created", dsn_host=settings.pg_host)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def upsert_many(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    conflict_columns: Sequence[str],
) -> int:
    """Insert many rows with ON CONFLICT DO NOTHING. Returns rows inserted (attempted)."""
    rows_list = list(rows)
    if not rows_list:
        return 0
    col_list = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    conflict_list = ", ".join(f'"{c}"' for c in conflict_columns)
    stmt = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_list}) DO NOTHING"
    )
    async with acquire() as conn:
        async with conn.transaction():
            await conn.executemany(stmt, rows_list)
    return len(rows_list)


async def bulk_upsert_via_copy(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    conflict_columns: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> int:
    """High-volume upsert via COPY → temp table → INSERT … ON CONFLICT.

    Use for backfills batching ~1000+ rows per call (Binance OHLCV
    pages, Coinalyze chunk loads). For ~10–100-row batches, plain
    `upsert_many` (executemany) is simpler and the COPY setup overhead
    isn't worth it; pick this path only when the batch size justifies
    the temp-table round-trip.

    `update_columns`:
        - ``None`` → ``ON CONFLICT … DO NOTHING`` (idempotent inserts)
        - non-empty → ``DO UPDATE SET col = EXCLUDED.col`` for each.

    Concurrency-safe: each call uses a uuid-suffixed temp table with
    ``ON COMMIT DROP``, so parallel pool connections don't collide.
    """
    rows_list = list(rows)
    if not rows_list:
        return 0
    col_list = ", ".join(f'"{c}"' for c in columns)
    conflict_list = ", ".join(f'"{c}"' for c in conflict_columns)
    if update_columns:
        update_clause = "DO UPDATE SET " + ", ".join(
            f'"{c}" = EXCLUDED."{c}"' for c in update_columns
        )
    else:
        update_clause = "DO NOTHING"
    temp_name = f"_bulk_upsert_{uuid.uuid4().hex[:12]}"

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f'CREATE TEMP TABLE "{temp_name}" '
                f"(LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            await conn.copy_records_to_table(temp_name, records=rows_list, columns=list(columns))
            await conn.execute(
                f"INSERT INTO {table} ({col_list}) "
                f'SELECT {col_list} FROM "{temp_name}" '
                f"ON CONFLICT ({conflict_list}) {update_clause}"
            )
    return len(rows_list)


async def fetch_latest_ts(table: str, where: str, params: Sequence[Any]) -> Any | None:
    async with acquire() as conn:
        return await conn.fetchval(f"SELECT max(ts) FROM {table} WHERE {where}", *params)
