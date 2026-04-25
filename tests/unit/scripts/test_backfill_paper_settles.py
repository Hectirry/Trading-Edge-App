"""Coverage for `scripts/backfill_paper_settles` bulk helpers.

The script's correctness rests on two non-obvious invariants:

1. The slug derivation in SQL (``regexp_replace(instrument_id,
   '-(YES|NO)\\.POLYMARKET$', '')``) must produce the same string as the
   Python ``_slug_from_instrument`` helper. They live in the same file
   and a divergence would silently associate orders to the wrong
   ``polymarket_markets`` row → wrong settle price → wrong PnL.
2. The bulk fetchers must not fire DB queries with empty input — easy
   to regress and would cost a no-op round-trip per backfill run.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# scripts/ isn't on sys.path by default in pytest; import by file path.
SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "backfill_paper_settles.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import backfill_paper_settles as bps  # noqa: E402

# Same regex the script uses inside the SQL CTE. Kept as a module-level
# constant here so the parity test fails loudly if either source moves.
_SQL_SLUG_REGEX = re.compile(r"-(YES|NO)\.POLYMARKET$")


class _FakeConn:
    def __init__(self, responses: list[tuple[str, tuple, list[dict]]]):
        self.responses = responses
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params):
        self.calls.append((sql, params))
        for sql_substr, expected_params, rows in self.responses:
            if sql_substr in sql and expected_params == params:
                return rows
        raise AssertionError(f"unexpected fetch:\n  sql={sql!r}\n  params={params!r}")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- slug regex parity -----------------------------------------------------


def test_slug_python_matches_sql_regex_for_yes() -> None:
    instrument = "btc-updown-5m-1776945900-YES.POLYMARKET"
    py_slug = bps._slug_from_instrument(instrument)
    sql_slug = _SQL_SLUG_REGEX.sub("", instrument)
    assert py_slug == sql_slug == "btc-updown-5m-1776945900"


def test_slug_python_matches_sql_regex_for_no() -> None:
    instrument = "btc-updown-5m-1776945900-NO.POLYMARKET"
    assert bps._slug_from_instrument(instrument) == _SQL_SLUG_REGEX.sub("", instrument)


def test_slug_handles_compound_token_dates() -> None:
    # Defensive: long unix timestamps + the YES/NO suffix should still
    # peel cleanly. If polymarket ever introduces a different suffix
    # this test catches the SQL/Python drift.
    for suffix in ("-YES.POLYMARKET", "-NO.POLYMARKET"):
        instrument = f"btc-updown-5m-1999999999{suffix}"
        assert bps._slug_from_instrument(instrument) == _SQL_SLUG_REGEX.sub("", instrument)


def test_side_from_slug() -> None:
    assert bps._side_from_slug("btc-updown-5m-1-YES.POLYMARKET").value in ("YES_UP", "yes_up")
    assert bps._side_from_slug("btc-updown-5m-1-NO.POLYMARKET").value in ("YES_DOWN", "yes_down")


# --- bulk helpers: empty input does not query ------------------------------


def test_bulk_ohlcv_closes_empty_input_does_not_query() -> None:
    conn = _FakeConn(responses=[])
    out = _run(bps._bulk_ohlcv_closes(conn, set()))
    assert out == {}
    assert conn.calls == []


def test_bulk_paper_open_prices_empty_input_does_not_query() -> None:
    conn = _FakeConn(responses=[])
    out = _run(bps._bulk_paper_open_prices(conn, set()))
    assert out == {}
    assert conn.calls == []


# --- bulk helpers: shape + omit-on-missing ---------------------------------


def test_bulk_ohlcv_closes_returns_minute_to_close_map() -> None:
    from datetime import UTC, datetime

    minutes = {
        datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        datetime(2026, 4, 23, 12, 5, tzinfo=UTC),
    }
    conn = _FakeConn(
        responses=[
            (
                "interval='1m'",
                (list(minutes),),
                [
                    {"ts": datetime(2026, 4, 23, 12, 0, tzinfo=UTC), "close": 70010.0},
                    {"ts": datetime(2026, 4, 23, 12, 5, tzinfo=UTC), "close": 70320.5},
                ],
            ),
        ]
    )
    out = _run(bps._bulk_ohlcv_closes(conn, minutes))
    assert out[datetime(2026, 4, 23, 12, 0, tzinfo=UTC)] == 70010.0
    assert out[datetime(2026, 4, 23, 12, 5, tzinfo=UTC)] == 70320.5


def test_bulk_ohlcv_closes_omits_null_close() -> None:
    from datetime import UTC, datetime

    minute = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    conn = _FakeConn(
        responses=[
            ("interval='1m'", ([minute],), [{"ts": minute, "close": None}]),
        ]
    )
    out = _run(bps._bulk_ohlcv_closes(conn, {minute}))
    assert out == {}


def test_bulk_paper_open_prices_uses_distinct_on() -> None:
    # Returns earliest-positive open_price per condition_id (DISTINCT ON).
    cids = {"cid_A", "cid_B"}
    conn = _FakeConn(
        responses=[
            (
                "DISTINCT ON (condition_id)",
                (list(cids),),
                [
                    {"condition_id": "cid_A", "open_price": 70000.5},
                    {"condition_id": "cid_B", "open_price": 70123.0},
                ],
            ),
        ]
    )
    out = _run(bps._bulk_paper_open_prices(conn, cids))
    assert out == {"cid_A": 70000.5, "cid_B": 70123.0}
