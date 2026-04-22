from trading.paper.feeds import (
    _best,
    _extract_chainlink_price,
    _sum_usd,
    compute_derived_book,
)
from trading.paper.state import CLOBBookSnapshot


def test_best_returns_highest_on_bid_side():
    assert _best([{"price": "0.51"}, {"price": "0.52"}, {"price": "0.50"}]) == 0.52


def test_best_returns_lowest_on_ask_side():
    assert (
        _best(
            [{"price": "0.53"}, {"price": "0.52"}, {"price": "0.54"}],
            ascending=True,
        )
        == 0.52
    )


def test_best_handles_zero_only():
    # All zeros → no positive price to return.
    assert _best([{"price": "0"}, {"price": "0.0"}]) == 0.0


def test_best_empty_input_returns_zero():
    assert _best([]) == 0.0


def test_sum_usd_accumulates_notional():
    levels = [
        {"price": "0.51", "size": "100"},
        {"price": "0.52", "size": "200"},
    ]
    assert abs(_sum_usd(levels) - (0.51 * 100 + 0.52 * 200)) < 1e-9


def test_compute_derived_book_basic():
    book = CLOBBookSnapshot(
        yes_bid=0.50,
        yes_ask=0.52,
        no_bid=0.48,
        no_ask=0.50,
        depth_yes=500,
        depth_no=250,
    )
    d = compute_derived_book(book)
    assert d["imbalance"] == 2.0
    # spread_bps ≈ (0.52 - 0.50) / 0.51 * 10000 ≈ 392
    assert 380 < d["spread_bps"] < 400
    assert 0.50 <= d["implied_prob_yes"] <= 0.52


def test_compute_derived_book_empty():
    book = CLOBBookSnapshot()
    d = compute_derived_book(book)
    assert d == {"imbalance": None, "spread_bps": None, "implied_prob_yes": None}


def test_extract_chainlink_price_nested():
    payload = {"envelope": {"data": [{"symbol": "btc/usd", "price": 95100.25}]}}
    assert _extract_chainlink_price(payload) == 95100.25


def test_extract_chainlink_price_ignores_small():
    # Prices < 1000 are filtered by the heuristic (avoid confusing sizes).
    payload = {"p": 50.0}
    assert _extract_chainlink_price(payload) is None
