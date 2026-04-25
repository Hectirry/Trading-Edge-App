"""Coverage for `common.db.bulk_upsert_via_copy`.

Tests the SQL contract and call sequence (CREATE TEMP → COPY → INSERT
ON CONFLICT) using a fake connection. Doesn't exercise real Postgres —
that would belong in an integration test — but locks the wire format so
typos in the templated SQL fail loud, and verifies:

- temp-table create/drop discipline (transaction-scoped, ON COMMIT DROP)
- DO NOTHING vs DO UPDATE conflict clauses
- empty input fast-paths to zero round-trips
- temp-table name is unique per call (so concurrent pool connections
  don't collide on the same temp name)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from trading.common import db as commondb


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []
        self.copy_calls: list[tuple[str, list, list[str]]] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql: str, *args):
        self.executed.append(sql)

    async def copy_records_to_table(self, table: str, *, records, columns):
        self.copy_calls.append((table, list(records), list(columns)))


@asynccontextmanager
async def _fake_acquire_factory(conn: _FakeConn):
    @asynccontextmanager
    async def _fake_acquire():
        yield conn

    yield _fake_acquire


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def conn() -> _FakeConn:
    return _FakeConn()


def test_empty_rows_skips_all_db_calls(conn: _FakeConn) -> None:
    with patch.object(commondb, "acquire", lambda: _FakeTransaction()):
        # acquire() shouldn't even be called when rows is empty —
        # but if it is, _FakeTransaction yields nothing useful.
        n = _run(
            commondb.bulk_upsert_via_copy(
                "market_data.foo",
                ["a", "b"],
                rows=[],
                conflict_columns=["a"],
            )
        )
    assert n == 0
    assert conn.executed == []
    assert conn.copy_calls == []


def test_do_nothing_path_emits_correct_sql(conn: _FakeConn, monkeypatch) -> None:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    monkeypatch.setattr(commondb, "acquire", fake_acquire)
    rows = [("binance", "BTCUSDT", "1m", "ts1", 1, 2, 3, 4, 5)]
    n = _run(
        commondb.bulk_upsert_via_copy(
            "market_data.crypto_ohlcv",
            ["exchange", "symbol", "interval", "ts", "open", "high", "low", "close", "volume"],
            rows=rows,
            conflict_columns=["exchange", "symbol", "interval", "ts"],
        )
    )
    assert n == 1
    assert len(conn.executed) == 2  # CREATE TEMP + INSERT
    create_sql, insert_sql = conn.executed
    assert "CREATE TEMP TABLE" in create_sql
    assert "LIKE market_data.crypto_ohlcv INCLUDING DEFAULTS" in create_sql
    assert "ON COMMIT DROP" in create_sql
    assert "INSERT INTO market_data.crypto_ohlcv" in insert_sql
    assert "ON CONFLICT" in insert_sql
    assert "DO NOTHING" in insert_sql
    assert "DO UPDATE" not in insert_sql
    # COPY went to the temp table.
    assert len(conn.copy_calls) == 1
    temp_table, copied_rows, copied_cols = conn.copy_calls[0]
    assert temp_table.startswith("_bulk_upsert_")
    assert copied_rows == rows
    assert copied_cols == [
        "exchange",
        "symbol",
        "interval",
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]


def test_do_update_path_emits_excluded_assignments(conn: _FakeConn, monkeypatch) -> None:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    monkeypatch.setattr(commondb, "acquire", fake_acquire)
    n = _run(
        commondb.bulk_upsert_via_copy(
            "market_data.liquidation_clusters",
            ["ts", "symbol", "side", "price", "size_usd", "source"],
            rows=[("t1", "BTCUSDT", "long", 70000, 1000, "coinalyze")],
            conflict_columns=["symbol", "side", "price", "ts"],
            update_columns=["size_usd"],
        )
    )
    assert n == 1
    insert_sql = conn.executed[1]
    assert "DO UPDATE SET" in insert_sql
    assert '"size_usd" = EXCLUDED."size_usd"' in insert_sql
    assert "DO NOTHING" not in insert_sql


def test_temp_table_name_changes_across_calls(monkeypatch) -> None:
    seen_names: list[str] = []

    class _Capturer(_FakeConn):
        async def execute(self, sql: str, *args):
            await super().execute(sql, *args)
            if "CREATE TEMP TABLE" in sql:
                # Pull the name out of the SQL string.
                quoted = sql.split('"', 2)
                seen_names.append(quoted[1])

    capturer = _Capturer()

    @asynccontextmanager
    async def fake_acquire():
        yield capturer

    monkeypatch.setattr(commondb, "acquire", fake_acquire)
    for _ in range(5):
        _run(
            commondb.bulk_upsert_via_copy(
                "market_data.foo",
                ["a"],
                rows=[(1,)],
                conflict_columns=["a"],
            )
        )
    assert len(set(seen_names)) == 5, f"expected 5 unique temp names, got: {seen_names}"
    assert all(n.startswith("_bulk_upsert_") for n in seen_names)
