"""Kraken WS v2 parsing tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from trading.ingest.kraken.adapter import (
    INTERVAL_TO_KRAKEN_MIN,
    KrakenAdapter,
    _from_kraken_pair,
    _to_kraken_pair,
)


def test_pair_translation() -> None:
    assert _to_kraken_pair("BTCUSD") == "XBTUSD"
    assert _to_kraken_pair("ETHUSD") == "ETHUSD"
    assert _to_kraken_pair("XBTUSD") == "XBTUSD"  # idempotent
    # Reverse:
    assert _from_kraken_pair("XBTUSD") == "BTCUSD"
    assert _from_kraken_pair("XXBTZUSD") == "BTCUSD"  # legacy
    assert _from_kraken_pair("ETHUSD") == "ETHUSD"


def test_interval_map() -> None:
    assert INTERVAL_TO_KRAKEN_MIN["1m"] == 1
    assert INTERVAL_TO_KRAKEN_MIN["1h"] == 60
    assert INTERVAL_TO_KRAKEN_MIN["1d"] == 1440


@pytest.mark.asyncio
async def test_handle_ohlc_update_persists_close() -> None:
    a = KrakenAdapter()
    msg = {
        "channel": "ohlc",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "interval": 1,
                "interval_begin": "2026-04-26T12:34:00.000000Z",
                "open": "67500.0",
                "high": "67550.0",
                "low": "67480.0",
                "close": "67520.0",
                "volume": "1.234",
            }
        ],
    }
    captured: dict = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["rows"] = rows
        return len(rows)

    with patch("trading.ingest.kraken.adapter.upsert_many", _fake_upsert):
        await a._handle_ohlc(msg)
    await a.aclose()

    row = captured["rows"][0]
    assert row[0] == "kraken"
    assert row[1] == "BTCUSD"  # canonical, no slash, no XBT
    assert row[2] == "1m"
    assert row[7] == Decimal("67520.0")  # close


@pytest.mark.asyncio
async def test_handle_ohlc_snapshot_skipped() -> None:
    """Kraken's initial snapshot for a subscription should not write rows."""
    a = KrakenAdapter()
    msg = {
        "channel": "ohlc",
        "type": "snapshot",
        "data": [
            {
                "symbol": "XBT/USD",
                "interval": 1,
                "interval_begin": "2026-04-26T12:34:00.000000Z",
                "open": "67500.0",
                "high": "67550.0",
                "low": "67480.0",
                "close": "67520.0",
                "volume": "1.234",
            }
        ],
    }
    captured: dict = {}

    async def _fake_upsert(*args, **kwargs):
        captured["called"] = True
        return 1

    with patch("trading.ingest.kraken.adapter.upsert_many", _fake_upsert):
        await a._handle_ohlc(msg)
    await a.aclose()
    assert "called" not in captured


@pytest.mark.asyncio
async def test_handle_trade_persists() -> None:
    a = KrakenAdapter()
    msg = {
        "channel": "trade",
        "data": [
            {
                "trade_id": 999,
                "symbol": "XBT/USD",
                "side": "buy",
                "price": "67510.50",
                "qty": "0.005",
                "timestamp": "2026-04-26T12:34:56.789Z",
            }
        ],
    }
    captured: dict = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["rows"] = rows
        return len(rows)

    with patch("trading.ingest.kraken.adapter.upsert_many", _fake_upsert):
        await a._handle_trade(msg)
    await a.aclose()

    row = captured["rows"][0]
    assert row[0] == "kraken"
    assert row[1] == "BTCUSD"
    assert row[3] == "999"
    assert row[6] == "buy"
