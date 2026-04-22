from trading.engine.types import Side
from trading.paper.exec_client import (
    LATE_ENTRY_GUARD,
    STALE_BOOK_SECONDS,
    _client_order_id,
)


def test_client_order_id_deterministic():
    a = _client_order_id("imbalance_v3", "btc-updown-5m-1", 1234.5, Side.YES_UP)
    b = _client_order_id("imbalance_v3", "btc-updown-5m-1", 1234.5, Side.YES_UP)
    assert a == b
    assert len(a) == 16


def test_client_order_id_changes_with_side():
    a = _client_order_id("s", "slug", 1.0, Side.YES_UP)
    b = _client_order_id("s", "slug", 1.0, Side.YES_DOWN)
    assert a != b


def test_late_entry_guard_is_five_seconds():
    assert LATE_ENTRY_GUARD == 5.0


def test_stale_book_threshold_is_ten_seconds():
    assert STALE_BOOK_SECONDS == 10.0
