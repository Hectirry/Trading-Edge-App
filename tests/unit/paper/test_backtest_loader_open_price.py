"""Coverage for `PaperTicksLoader`'s bulk OHLCV/settle helpers.

Locks in the forensic invariant from
``estrategias/en-desarrollo/_forensics_trend_confirm_t1_v1.md``: window
opens come from the Binance 1m **open** at ``ts == window_close - 300``,
NOT the 5m close (which is the price at window_close, not window_open
— that off-by-one fed sign-flipped delta_bps and 9.7% win rates).

Also locks the bulk semantics (single round-trip per helper) and the
"omit-on-missing-data" contract that the loader and `run_backtest` rely
on to skip incomplete markets.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from trading.paper.backtest_loader import _bulk_ohlcv_opens, _bulk_settle_prices


class _FakeConn:
    """Minimal asyncpg-like fake. Matches by SQL substring + params and
    raises on unexpected queries so silent fallthroughs fail loud."""

    def __init__(self, responses: list[tuple[str, tuple, list[dict]]]):
        self.responses = responses
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params):
        self.calls.append((sql, params))
        for sql_substr, expected_params, rows in self.responses:
            if sql_substr in sql and expected_params == params:
                return rows
        raise AssertionError(
            f"unexpected fetch:\n  sql={sql!r}\n  params={params!r}\n"
            f"  responses_keys={[(s, p) for s, p, _ in self.responses]}"
        )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- _bulk_ohlcv_opens -----------------------------------------------------


def test_bulk_opens_returns_one_minute_open_at_window_open() -> None:
    # window_close=12:05 UTC → window_open=12:00 UTC. The 1m candle at
    # ts=12:00:00 has open=70010.0 — the price at exactly 12:00.
    window_close_ts = 1776945900  # 2026-04-23 12:05:00 UTC
    open_minute = datetime.fromtimestamp(window_close_ts - 300, tz=UTC)
    conn = _FakeConn(
        responses=[
            (
                "interval='1m'",
                ([open_minute],),
                [{"open_minute": window_close_ts - 300, "open": 70010.0}],
            ),
        ]
    )
    out = _run(_bulk_ohlcv_opens(conn, [window_close_ts]))
    assert out == {window_close_ts: 70010.0}
    # Forensic invariant: still 1m, never 5m.
    assert all("interval='1m'" in s and "interval='5m'" not in s for s, _ in conn.calls)


def test_bulk_opens_one_round_trip_for_many_markets() -> None:
    # The whole point of the bulk variant: N markets → 1 query.
    close_tss = [1776945900, 1776946200, 1776946500]  # three back-to-back 5m windows
    open_minutes = [datetime.fromtimestamp(c - 300, tz=UTC) for c in close_tss]
    conn = _FakeConn(
        responses=[
            (
                "interval='1m'",
                (open_minutes,),
                [
                    {"open_minute": close_tss[0] - 300, "open": 70010.0},
                    {"open_minute": close_tss[1] - 300, "open": 70015.5},
                    {"open_minute": close_tss[2] - 300, "open": 70020.0},
                ],
            ),
        ]
    )
    out = _run(_bulk_ohlcv_opens(conn, close_tss))
    assert out == {
        close_tss[0]: 70010.0,
        close_tss[1]: 70015.5,
        close_tss[2]: 70020.0,
    }
    assert len(conn.calls) == 1


def test_bulk_opens_omits_markets_without_a_candle() -> None:
    # Ingest gap: middle market's candle is missing → it's omitted.
    # Caller (loader) falls back to the row's spot_price for that one.
    close_tss = [1776945900, 1776946200, 1776946500]
    open_minutes = [datetime.fromtimestamp(c - 300, tz=UTC) for c in close_tss]
    conn = _FakeConn(
        responses=[
            (
                "interval='1m'",
                (open_minutes,),
                [
                    {"open_minute": close_tss[0] - 300, "open": 70010.0},
                    # close_tss[1] missing
                    {"open_minute": close_tss[2] - 300, "open": 70020.0},
                ],
            ),
        ]
    )
    out = _run(_bulk_ohlcv_opens(conn, close_tss))
    assert out == {close_tss[0]: 70010.0, close_tss[2]: 70020.0}
    assert close_tss[1] not in out


def test_bulk_opens_omits_rows_with_null_open() -> None:
    # NULL open is treated as missing; never coerced to 0.0 (which
    # would silently flip delta_bps signs in the loader).
    window_close_ts = 1776945900
    open_minute = datetime.fromtimestamp(window_close_ts - 300, tz=UTC)
    conn = _FakeConn(
        responses=[
            (
                "interval='1m'",
                ([open_minute],),
                [{"open_minute": window_close_ts - 300, "open": None}],
            ),
        ]
    )
    out = _run(_bulk_ohlcv_opens(conn, [window_close_ts]))
    assert out == {}


def test_bulk_opens_handles_empty_input_without_a_query() -> None:
    conn = _FakeConn(responses=[])  # any fetch call would raise
    out = _run(_bulk_ohlcv_opens(conn, []))
    assert out == {}
    assert conn.calls == []


# --- _bulk_settle_prices ---------------------------------------------------


def test_bulk_settle_returns_one_minute_close_at_market_close_minute() -> None:
    # Mirrors backfill_paper_settles._settle_price_at: 1m.close at the
    # minute-floor of polymarket_markets.close_time. The bulk version
    # does this for many slugs in one CTE join.
    slugs = ["btc-updown-5m-1776945900", "btc-updown-5m-1776946200"]
    conn = _FakeConn(
        responses=[
            (
                "FROM market_data.polymarket_markets",
                (slugs,),
                [
                    {"slug": slugs[0], "close": 70320.5},
                    {"slug": slugs[1], "close": 70355.0},
                ],
            ),
        ]
    )
    out = _run(_bulk_settle_prices(conn, slugs))
    assert out == {slugs[0]: 70320.5, slugs[1]: 70355.0}
    # Single round-trip — the whole point.
    assert len(conn.calls) == 1


def test_bulk_settle_omits_slugs_with_no_market_or_no_candle() -> None:
    # The CTE/JOIN naturally drops rows where polymarket_markets is
    # missing OR crypto_ohlcv has no row at the close minute. We assert
    # the contract: missing slugs are absent from the dict (caller
    # decides whether to skip or raise).
    slugs = ["btc-updown-5m-1776945900", "btc-updown-5m-9999999999"]
    conn = _FakeConn(
        responses=[
            (
                "FROM market_data.polymarket_markets",
                (slugs,),
                [{"slug": slugs[0], "close": 70320.5}],
            ),
        ]
    )
    out = _run(_bulk_settle_prices(conn, slugs))
    assert out == {slugs[0]: 70320.5}
    assert slugs[1] not in out


def test_bulk_settle_handles_empty_input_without_a_query() -> None:
    conn = _FakeConn(responses=[])
    out = _run(_bulk_settle_prices(conn, []))
    assert out == {}
    assert conn.calls == []
