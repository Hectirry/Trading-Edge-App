"""Verify the open_price override reads ``open`` from the Binance 1m
candle whose ``ts == window_close - 300`` (== window_open), and NOT the
``close`` of the 5m candle that covers the window. Regression for the
forensic recap in ``estrategias/en-desarrollo/_forensics_trend_confirm_t1_v1.md``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from trading.paper.backtest_loader import (
    _fetch_ohlcv_window_open,
    _fetch_settle_price_for_slug,
)


class _FakeConn:
    """Minimal asyncpg-like fake. Stores expected (sql_substr, params) →
    row map; raises on unexpected queries so silent fallthroughs fail loud.
    """

    def __init__(self, responses: list[tuple[str, tuple, dict | None]]):
        self.responses = responses
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *params):
        self.calls.append((sql, params))
        for sql_substr, expected_params, row in self.responses:
            if sql_substr in sql and expected_params == params:
                return row
        raise AssertionError(
            f"unexpected fetchrow:\n  sql={sql!r}\n  params={params!r}\n"
            f"  responses_keys={[(s, p) for s, p, _ in self.responses]}"
        )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_open_price_reads_one_minute_open_at_window_open() -> None:
    # window_close=12:05 UTC → window_open=12:00 UTC. The 1m candle at
    # ts=12:00:00 has open=70010.0 (price exactly at 12:00) and close=
    # 70015.0 (price at 12:01). Loader must return 70010.0, the open.
    window_close_ts = 1776945900  # 2026-04-23 12:05:00 UTC
    one_minute_row = {"open": 70010.0, "close": 70015.0}
    conn = _FakeConn(
        responses=[
            ("interval='1m'", (window_close_ts - 300,), one_minute_row),
        ]
    )
    out = _run(_fetch_ohlcv_window_open(conn, window_close_ts))
    assert out == 70010.0
    # Confirm the SQL did NOT request the 5m close path.
    assert all("interval='1m'" in s for s, _ in conn.calls)


def test_open_price_returns_none_on_ingest_gap() -> None:
    # No candle for that minute → return None; caller decides fallback.
    window_close_ts = 1776945900
    conn = _FakeConn(
        responses=[
            ("interval='1m'", (window_close_ts - 300,), None),
        ]
    )
    out = _run(_fetch_ohlcv_window_open(conn, window_close_ts))
    assert out is None


def test_open_price_returns_none_when_open_column_is_null() -> None:
    # Row exists but open is NULL → still return None, do not coerce to 0.
    window_close_ts = 1776945900
    conn = _FakeConn(
        responses=[
            ("interval='1m'", (window_close_ts - 300,), {"open": None}),
        ]
    )
    out = _run(_fetch_ohlcv_window_open(conn, window_close_ts))
    assert out is None


def test_settle_price_uses_one_minute_close_at_market_close_minute() -> None:
    # Mirror backfill_paper_settles._settle_price_at: minute floor of
    # close_time, then 1m.close. Slug 1776945900 → close_time=12:05:00 UTC.
    close_dt = datetime(2026, 4, 23, 12, 5, 0, tzinfo=UTC)
    conn = _FakeConn(
        responses=[
            (
                "FROM market_data.polymarket_markets",
                ("btc-updown-5m-1776945900",),
                {"close_time": close_dt},
            ),
            (
                "FROM market_data.crypto_ohlcv",
                (close_dt,),  # already minute-floored
                {"close": 70320.5},
            ),
        ]
    )
    out = _run(_fetch_settle_price_for_slug(conn, "btc-updown-5m-1776945900"))
    assert out == 70320.5


def test_settle_price_returns_none_when_market_row_missing() -> None:
    conn = _FakeConn(
        responses=[
            (
                "FROM market_data.polymarket_markets",
                ("btc-updown-5m-1776945900",),
                None,
            ),
        ]
    )
    out = _run(_fetch_settle_price_for_slug(conn, "btc-updown-5m-1776945900"))
    assert out is None


def test_settle_price_returns_none_when_ohlcv_gap() -> None:
    close_dt = datetime(2026, 4, 23, 12, 5, 0, tzinfo=UTC)
    conn = _FakeConn(
        responses=[
            (
                "FROM market_data.polymarket_markets",
                ("btc-updown-5m-1776945900",),
                {"close_time": close_dt},
            ),
            (
                "FROM market_data.crypto_ohlcv",
                (close_dt,),
                None,
            ),
        ]
    )
    out = _run(_fetch_settle_price_for_slug(conn, "btc-updown-5m-1776945900"))
    assert out is None
