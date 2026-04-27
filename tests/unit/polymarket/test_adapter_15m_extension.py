"""Regression: adapter extension for btc-updown-15m must not break the 5m flow.

Tests stay hermetic — no real API. The 5m enumeration path still uses
`_fetch_events_page` (series_id=10684), the new 15m path uses
`_fetch_events_page_global`, and `_market_row_from_event` filters by the
slug_pattern its caller passed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from trading.ingest.polymarket.adapter import (
    GAMMA_EVENTS_PAGE_SIZE,
    SLUG_PREFIX_5M,
    SLUG_PREFIX_15M,
    SUPPORTED_SLUG_PATTERNS,
    PolymarketAdapter,
)


def test_supported_slug_patterns_cover_5m_and_15m():
    assert SLUG_PREFIX_5M == "btc-updown-5m-"
    assert SLUG_PREFIX_15M == "btc-updown-15m-"
    assert SUPPORTED_SLUG_PATTERNS == (SLUG_PREFIX_5M, SLUG_PREFIX_15M)


def test_discover_markets_rejects_unsupported_pattern():
    a = PolymarketAdapter()
    with pytest.raises(ValueError, match="unsupported slug_pattern"):
        asyncio.run(a.discover_markets("eth-updown-5m-", datetime.now(tz=UTC)))


def _fake_event_with_market(slug: str, condition_id: str, end_iso: str = "2026-04-22T00:00:00Z") -> dict:
    return {
        "endDate": end_iso,
        "markets": [
            {
                "conditionId": condition_id,
                "slug": slug,
                "question": "BTC up?",
                "closed": True,
            }
        ],
    }


def test_5m_enumeration_uses_series_path_and_ignores_15m_slugs():
    """The 5m path must call `_fetch_events_page` (with series_id), and its
    `_market_row_from_event` filter must reject any 15m slug that leaks in.
    """
    a = PolymarketAdapter()
    series_calls = []
    global_calls = []

    async def fake_series_page(offset):
        series_calls.append(offset)
        if offset == 0:
            return [
                _fake_event_with_market("btc-updown-5m-1776844800", "0xaaa"),
                _fake_event_with_market("btc-updown-15m-1776844800", "0xbbb"),  # leaked 15m
            ]
        return []

    async def fake_global_page(offset):
        global_calls.append(offset)
        return []

    async def fake_upsert(*args, **kwargs):
        return 0

    with patch.object(a, "_fetch_events_page", side_effect=fake_series_page), \
         patch.object(a, "_fetch_events_page_global", side_effect=fake_global_page), \
         patch("trading.ingest.polymarket.adapter.upsert_many", side_effect=fake_upsert) as up:
        asyncio.run(a.discover_markets(SLUG_PREFIX_5M, datetime(2026, 4, 22, tzinfo=UTC)))

    assert series_calls, "5m path must hit the series-filtered events page"
    assert not global_calls, "5m path must NOT hit the global events page"
    # upsert was called with rows — verify only the 5m row passed the filter.
    assert up.called
    args, _ = up.call_args
    rows = args[2]
    slugs = [r[1] for r in rows]
    assert "btc-updown-5m-1776844800" in slugs
    assert "btc-updown-15m-1776844800" not in slugs, "15m slug must not leak through 5m filter"


def test_15m_enumeration_uses_global_path_and_filters_other_slugs():
    """The 15m path uses `_fetch_events_page_global` and filters non-15m slugs."""
    a = PolymarketAdapter()
    series_calls = []
    global_calls = []

    async def fake_global_page(offset):
        global_calls.append(offset)
        if offset == 0:
            return [
                _fake_event_with_market("btc-updown-15m-1776844800", "0x111"),
                _fake_event_with_market("eth-updown-5m-1776844800", "0x222"),  # noise
                _fake_event_with_market("btc-updown-5m-1776844800", "0x333"),  # noise
            ]
        return []

    async def fake_series_page(offset):
        series_calls.append(offset)
        return []

    async def fake_upsert(*args, **kwargs):
        return 0

    with patch.object(a, "_fetch_events_page", side_effect=fake_series_page), \
         patch.object(a, "_fetch_events_page_global", side_effect=fake_global_page), \
         patch("trading.ingest.polymarket.adapter.upsert_many", side_effect=fake_upsert) as up:
        asyncio.run(a.discover_markets(SLUG_PREFIX_15M, datetime(2026, 4, 22, tzinfo=UTC)))

    assert global_calls, "15m path must hit the global events page"
    assert not series_calls, "15m path must NOT hit the series-filtered events page"
    assert up.called
    args, _ = up.call_args
    rows = args[2]
    slugs = [r[1] for r in rows]
    assert slugs == ["btc-updown-15m-1776844800"], f"only 15m slugs allowed, got {slugs}"


def test_market_row_from_event_filters_by_pattern_default_is_5m():
    """Default slug_pattern argument keeps 5m semantics (backward-compat)."""
    a = PolymarketAdapter()
    ev_5m = _fake_event_with_market("btc-updown-5m-1776844800", "0xaaa")
    ev_15m = _fake_event_with_market("btc-updown-15m-1776844800", "0xbbb")

    # default arg = 5m
    assert a._market_row_from_event(ev_5m["markets"][0], ev_5m) is not None
    assert a._market_row_from_event(ev_15m["markets"][0], ev_15m) is None

    # explicit 15m
    assert a._market_row_from_event(ev_15m["markets"][0], ev_15m, SLUG_PREFIX_15M) is not None
    assert a._market_row_from_event(ev_5m["markets"][0], ev_5m, SLUG_PREFIX_15M) is None


def test_backfill_market_trades_paginates_and_dedupes(monkeypatch):
    """`backfill_market_trades` must paginate via offset until short page,
    drop trades missing required fields, and pass `transactionHash` as PK
    component to upsert_many.
    """
    a = PolymarketAdapter()
    seen_offsets: list[int] = []
    # page_size=2 keeps the fixture small. Page 0 returns 2 (=page_size) so the
    # loop must paginate; page at offset=2 returns 1 (<page_size) so it stops.
    pages = {
        0: [
            {
                "transactionHash": "0xtx1",
                "asset": "tokA",
                "timestamp": 1777303014,
                "price": 0.999,
                "size": 6.76,
                "side": "SELL",
                "conditionId": "0xCOND",
            },
            {
                # missing transactionHash → must be dropped
                "asset": "tokA",
                "timestamp": 1777303013,
                "price": 0.5,
                "size": 1.0,
                "side": "BUY",
                "conditionId": "0xCOND",
            },
        ],
        2: [
            {
                "transactionHash": "0xtx2",
                "asset": "tokB",
                "timestamp": 1777303015,
                "price": 0.001,
                "size": 12.0,
                "side": "BUY",
                "conditionId": "0xCOND",
            }
        ],  # short page → stops after this
    }

    async def fake_page(condition_id, offset, limit):
        seen_offsets.append(offset)
        return pages.get(offset, [])

    captured: list = []

    async def fake_upsert(table, cols, rows, pk):
        captured.append({"table": table, "cols": cols, "rows": rows, "pk": pk})
        return len(rows)

    monkeypatch.setattr(a, "_fetch_trades_page", fake_page)
    monkeypatch.setattr("trading.ingest.polymarket.adapter.upsert_many", fake_upsert)

    n = asyncio.run(a.backfill_market_trades("0xCOND", page_size=2))
    assert n == 2, f"two valid trades expected (one dropped for missing tx_hash), got {n}"
    assert seen_offsets == [0, 2]
    assert all(c["table"] == "market_data.polymarket_trades" for c in captured)
    # PK includes transactionHash
    assert captured[0]["pk"] == ["condition_id", "tx_hash", "ts"]
