import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.ingest.binance.adapter import INTERVAL_MS

FIX = Path(__file__).parent.parent.parent / "fixtures" / "binance"


def test_interval_ms_matches_spec():
    assert INTERVAL_MS["1m"] == 60_000
    assert INTERVAL_MS["5m"] == 300_000
    assert INTERVAL_MS["1h"] == 3_600_000
    assert INTERVAL_MS["1d"] == 86_400_000


def test_kline_fixture_shape():
    raw = json.loads((FIX / "klines_btcusdt_5m.json").read_text())
    assert len(raw) == 3
    first = raw[0]
    assert first[0] == 1735689600000
    assert Decimal(first[1]) == Decimal("95000.00")
    expected_ts = datetime.fromtimestamp(first[0] / 1000, tz=UTC)
    assert expected_ts == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)


def test_aggtrade_ws_fixture_fields():
    msg = json.loads((FIX / "aggtrade_btcusdt_ws.json").read_text())
    data = msg["data"]
    assert data["s"] == "BTCUSDT"
    assert Decimal(data["p"]) == Decimal("95100.50")
    # m=false means buyer is maker; taker side is buy
    assert data["m"] is False
