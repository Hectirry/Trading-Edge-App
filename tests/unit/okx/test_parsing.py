"""OKX V5 parsing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from trading.ingest.okx.adapter import (
    INTERVAL_TO_BAR,
    OkxAdapter,
    _to_okx_inst_id,
)


def test_inst_id_translation() -> None:
    assert _to_okx_inst_id("BTCUSDT") == "BTC-USDT"
    assert _to_okx_inst_id("ETHUSDT") == "ETH-USDT"
    assert _to_okx_inst_id("BTC-USDT") == "BTC-USDT"
    assert _to_okx_inst_id("BTCUSD") == "BTC-USD"


def test_interval_map() -> None:
    assert INTERVAL_TO_BAR["1m"] == "1m"
    assert INTERVAL_TO_BAR["1h"] == "1H"


@pytest.mark.asyncio
async def test_handle_candle_skips_in_progress() -> None:
    a = OkxAdapter()
    # confirm flag '0' = in progress; should be skipped.
    msg = {
        "data": [
            ["1700000000000", "67500", "67550", "67480", "67520", "1.234", "0", "0", "0"]
        ]
    }
    captured: dict = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["called"] = True
        return len(rows)

    with patch("trading.ingest.okx.adapter.upsert_many", _fake_upsert):
        await a._handle_candle({"channel": "candle1m", "instId": "BTC-USDT"}, msg)
    await a.aclose()
    assert "called" not in captured  # nothing upserted on in-progress bar


@pytest.mark.asyncio
async def test_handle_candle_persists_closed() -> None:
    a = OkxAdapter()
    msg = {
        "data": [
            ["1700000000000", "67500", "67550", "67480", "67520", "1.234", "0", "0", "1"]
        ]
    }
    captured: dict = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["rows"] = rows
        return len(rows)

    with patch("trading.ingest.okx.adapter.upsert_many", _fake_upsert):
        await a._handle_candle({"channel": "candle1m", "instId": "BTC-USDT"}, msg)
    await a.aclose()

    assert captured["rows"][0][0] == "okx"
    assert captured["rows"][0][1] == "BTCUSDT"
    assert captured["rows"][0][2] == "1m"
    assert captured["rows"][0][3] == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert captured["rows"][0][4] == Decimal("67500")


@pytest.mark.asyncio
async def test_handle_trade_lowercases_side() -> None:
    a = OkxAdapter()
    msg = {
        "data": [
            {
                "tradeId": "999",
                "px": "67510.50",
                "sz": "0.005",
                "side": "buy",
                "ts": "1700000000123",
            }
        ]
    }
    captured: dict = {}

    async def _fake_upsert(table, columns, rows, conflict):
        captured["rows"] = rows
        return len(rows)

    with patch("trading.ingest.okx.adapter.upsert_many", _fake_upsert):
        await a._handle_trade({"instId": "BTC-USDT"}, msg)
    await a.aclose()

    row = captured["rows"][0]
    assert row[0] == "okx"
    assert row[1] == "BTCUSDT"
    assert row[6] == "buy"
