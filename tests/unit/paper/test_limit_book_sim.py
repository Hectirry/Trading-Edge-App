"""LimitBookSim unit tests (Phase 3.8a). All tests run with persist=False
to avoid hitting the database."""

from __future__ import annotations

import pytest

from trading.paper.limit_book_sim import (
    LimitBookSim,
    LimitOrder,
    deterministic_coid,
)


def _order(**overrides) -> LimitOrder:
    base = dict(
        coid="coid-abc",
        strategy_id="grid_test",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        price=100.0,
        qty=1.0,
        ts_placed=1_000_000.0,
        ttl_s=None,
        metadata={},
    )
    base.update(overrides)
    return LimitOrder(**base)


def _book() -> LimitBookSim:
    return LimitBookSim(persist=False)


async def test_place_and_get() -> None:
    b = _book()
    ok = await b.place(_order(coid="c1"))
    assert ok is True
    assert len(b) == 1
    assert b.get("c1") is not None


async def test_duplicate_coid_rejected() -> None:
    b = _book()
    assert await b.place(_order(coid="c1")) is True
    # Second place with same coid is a no-op.
    assert await b.place(_order(coid="c1", price=99.0)) is False
    # Original price preserved.
    assert b.get("c1").price == 100.0


async def test_cancel_removes_order() -> None:
    b = _book()
    await b.place(_order(coid="c1"))
    ok = await b.cancel("c1")
    assert ok is True
    assert len(b) == 0
    # Second cancel is idempotent false.
    assert await b.cancel("c1") is False


async def test_cancel_all_by_strategy() -> None:
    b = _book()
    await b.place(_order(coid="a1", strategy_id="s1"))
    await b.place(_order(coid="a2", strategy_id="s1"))
    await b.place(_order(coid="b1", strategy_id="s2"))
    n = await b.cancel_all(strategy_id="s1", reason="reset")
    assert n == 2
    assert b.get("a1") is None and b.get("a2") is None
    assert b.get("b1") is not None


async def test_buy_fills_when_price_dips_to_level() -> None:
    b = _book()
    await b.place(_order(coid="c1", side="BUY", price=100.0))
    # px > level — no fill.
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=101.0, ts=1.0)
    assert fills == []
    # px at level — fill.
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=100.0, ts=2.0)
    assert len(fills) == 1
    assert fills[0].coid == "c1"
    assert fills[0].price == 100.0
    # Order removed from book after fill.
    assert b.get("c1") is None


async def test_sell_fills_when_price_rises_to_level() -> None:
    b = _book()
    await b.place(_order(coid="c1", side="SELL", price=110.0))
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=109.0, ts=1.0)
    assert fills == []
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=110.5, ts=2.0)
    assert len(fills) == 1
    assert fills[0].side == "SELL"


async def test_fee_applied_in_bps() -> None:
    b = LimitBookSim(persist=False, maker_fee_bps=10.0)
    await b.place(_order(coid="c1", side="BUY", price=100.0, qty=2.0))
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=99.0, ts=1.0)
    assert len(fills) == 1
    # 10 bps * 100 * 2 = 0.2
    assert pytest.approx(fills[0].fee, rel=1e-9) == 0.2


async def test_multiple_fills_in_one_tick_fifo_order() -> None:
    b = _book()
    await b.place(_order(coid="older", side="BUY", price=100.0, ts_placed=1.0))
    await b.place(_order(coid="newer", side="BUY", price=105.0, ts_placed=2.0))
    # Tick at 99 crosses both.
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=99.0, ts=3.0)
    assert [f.coid for f in fills] == ["older", "newer"]


async def test_other_instrument_ignored() -> None:
    b = _book()
    await b.place(_order(coid="c1", instrument_id="ETHUSDT.BINANCE", side="BUY", price=100.0))
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=50.0, ts=1.0)
    assert fills == []
    assert b.get("c1") is not None


async def test_ttl_expiry_drops_without_fill() -> None:
    b = _book()
    await b.place(_order(coid="c1", ttl_s=60.0, ts_placed=1_000.0))
    # Tick inside TTL.
    await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=200.0, ts=1_030.0)
    assert b.get("c1") is not None
    # Tick past TTL — order evicted, no fill reported.
    fills = await b.on_tick(instrument_id="BTCUSDT.BINANCE", px=200.0, ts=1_061.0)
    assert fills == []
    assert b.get("c1") is None


async def test_reject_invalid_side() -> None:
    b = _book()
    with pytest.raises(ValueError):
        await b.place(_order(coid="bad", side="HOLD"))


async def test_reject_non_positive_price_or_qty() -> None:
    b = _book()
    with pytest.raises(ValueError):
        await b.place(_order(coid="bad", price=0.0))
    with pytest.raises(ValueError):
        await b.place(_order(coid="bad2", qty=-1.0))


def test_deterministic_coid_stable() -> None:
    a = deterministic_coid(
        strategy_id="s",
        instrument_id="BTCUSDT.BINANCE",
        reset_gen=0,
        level_idx=-3,
        side="BUY",
        center_price=75000.0,
    )
    b = deterministic_coid(
        strategy_id="s",
        instrument_id="BTCUSDT.BINANCE",
        reset_gen=0,
        level_idx=-3,
        side="BUY",
        center_price=75000.0,
    )
    assert a == b
    # Different reset_gen → different coid.
    c = deterministic_coid(
        strategy_id="s",
        instrument_id="BTCUSDT.BINANCE",
        reset_gen=1,
        level_idx=-3,
        side="BUY",
        center_price=75000.0,
    )
    assert a != c
    # Different center → different coid.
    d = deterministic_coid(
        strategy_id="s",
        instrument_id="BTCUSDT.BINANCE",
        reset_gen=0,
        level_idx=-3,
        side="BUY",
        center_price=76000.0,
    )
    assert a != d


async def test_snapshot_returns_list_copy() -> None:
    b = _book()
    await b.place(_order(coid="c1"))
    snap = b.snapshot()
    assert len(snap) == 1
    # Mutating the returned list does not affect internal state.
    snap.clear()
    assert len(b) == 1
