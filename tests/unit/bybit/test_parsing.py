import json
from decimal import Decimal
from pathlib import Path

from trading.ingest.bybit.adapter import INTERVAL_TO_BYBIT

FIX = Path(__file__).parent.parent.parent / "fixtures" / "bybit"


def test_interval_map_covers_spec():
    for i in ("1m", "5m", "15m", "1h", "1d"):
        assert i in INTERVAL_TO_BYBIT


def test_kline_rest_fixture_newest_first():
    raw = json.loads((FIX / "klines_btcusdt_5m.json").read_text())
    assert raw["retCode"] == 0
    lst = raw["result"]["list"]
    # Bybit returns newest first. Adapter reverses internally.
    assert int(lst[0][0]) > int(lst[-1][0])


def test_kline_ws_fixture_parses():
    msg = json.loads((FIX / "kline_ws.json").read_text())
    k = msg["data"][0]
    assert k["confirm"] is True
    assert Decimal(k["open"]) == Decimal("95000.00")


def test_trade_ws_fixture_side_mapping():
    msg = json.loads((FIX / "trade_ws.json").read_text())
    t = msg["data"][0]
    assert t["S"] == "Buy"
    assert t["i"] == "abc-123-xyz"
