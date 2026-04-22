import json
from decimal import Decimal
from pathlib import Path

from trading.ingest.polymarket.adapter import PolymarketAdapter

FIX = Path(__file__).parent.parent.parent / "fixtures" / "polymarket"


def test_extract_tokens_handles_json_string():
    meta = {"clobTokenIds": '["a", "b"]'}
    assert PolymarketAdapter._extract_tokens(meta) == ["a", "b"]


def test_extract_tokens_handles_objects():
    meta = {"tokens": [{"token_id": "x"}, {"token_id": "y"}]}
    assert PolymarketAdapter._extract_tokens(meta) == ["x", "y"]


def test_extract_tokens_empty_on_missing():
    assert PolymarketAdapter._extract_tokens({}) == []


def test_mid_from_book_fixture():
    msg = json.loads((FIX / "clob_book_ws.json").read_text())
    mid = PolymarketAdapter._mid_from_book(msg)
    assert mid == Decimal("0.52")


def test_market_row_from_gamma():
    markets = json.loads((FIX / "market_gamma.json").read_text())
    row = PolymarketAdapter._market_row(markets[0], window_ts_fallback=1735690200)
    condition_id, slug, question, window_ts, resolved, outcome = row[:6]
    assert condition_id == "0xabc123deadbeef"
    assert slug == "btc-updown-5m-1735690200"
    assert window_ts == 1735690200
    assert resolved is False
    assert outcome is None
