"""Coinbase Advanced Trade WS / REST parsing tests.

These exercise the pure-data paths only — no live WS or REST. The
upsert hooks are stubbed to capture the row tuple shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from trading.ingest.coinbase.adapter import (
    INTERVAL_TO_GRANULARITY,
    CoinbaseAdapter,
    _to_product_id,
)


def test_product_id_translation() -> None:
    assert _to_product_id("BTCUSD") == "BTC-USD"
    assert _to_product_id("ETHUSDT") == "ETH-USDT"
    assert _to_product_id("BTC-USD") == "BTC-USD"  # idempotent
    assert _to_product_id("btcusd") == "BTC-USD"  # case-insensitive


def test_interval_granularity_map_covers_canonical() -> None:
    for interval in ("1m", "5m", "15m", "1h", "1d"):
        assert interval in INTERVAL_TO_GRANULARITY
        assert INTERVAL_TO_GRANULARITY[interval] in (60, 300, 900, 3600, 86400)


@pytest.mark.asyncio
async def test_handle_candles_writes_canonical_row() -> None:
    adapter = CoinbaseAdapter()
    msg = {
        "channel": "candles",
        "events": [
            {
                "candles": [
                    {
                        "start": 1_700_000_000,
                        "open": "67500.00",
                        "high": "67550.00",
                        "low": "67480.00",
                        "close": "67520.00",
                        "volume": "1.234",
                        "product_id": "BTC-USD",
                    }
                ]
            }
        ],
    }
    captured = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["table"] = table
        captured["columns"] = tuple(columns)
        captured["rows"] = rows
        captured["conflict"] = tuple(conflict)
        return len(rows)

    with patch("trading.ingest.coinbase.adapter.upsert_many", _fake_upsert):
        await adapter._handle_candles(msg)
    await adapter.aclose()

    assert captured["table"] == "market_data.crypto_ohlcv"
    assert captured["columns"][0] == "exchange"
    rows = captured["rows"]
    assert len(rows) == 1
    row = rows[0]
    # exchange, symbol, interval, ts, o, h, l, c, v
    assert row[0] == "coinbase"
    assert row[1] == "BTCUSD"
    assert row[2] == "1m"
    assert row[3] == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert row[4] == Decimal("67500.00")
    assert row[5] == Decimal("67550.00")
    assert row[6] == Decimal("67480.00")
    assert row[7] == Decimal("67520.00")
    assert row[8] == Decimal("1.234")


@pytest.mark.asyncio
async def test_handle_trades_translates_side_and_strips_hyphen() -> None:
    adapter = CoinbaseAdapter()
    msg = {
        "channel": "market_trades",
        "events": [
            {
                "trades": [
                    {
                        "trade_id": "999",
                        "product_id": "BTC-USD",
                        "price": "67510.50",
                        "size": "0.005",
                        "side": "BUY",
                        "time": "2026-04-26T12:34:56.789Z",
                    },
                    {
                        "trade_id": "1000",
                        "product_id": "BTC-USD",
                        "price": "67510.00",
                        "size": "0.010",
                        "side": "SELL",
                        "time": "2026-04-26T12:34:57.123Z",
                    },
                ]
            }
        ],
    }
    captured = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["table"] = table
        captured["rows"] = rows
        return len(rows)

    with patch("trading.ingest.coinbase.adapter.upsert_many", _fake_upsert):
        await adapter._handle_trades(msg)
    await adapter.aclose()

    rows = captured["rows"]
    assert len(rows) == 2
    # exchange, symbol, ts, trade_id, price, qty, side
    assert rows[0][0] == "coinbase"
    assert rows[0][1] == "BTCUSD"  # hyphen stripped
    assert rows[0][3] == "999"
    assert rows[0][6] == "buy"  # uppercase BUY → lowercase
    assert rows[1][6] == "sell"
    # ts parsed correctly
    assert rows[0][2].tzinfo is not None
