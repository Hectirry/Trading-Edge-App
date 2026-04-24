"""grid_base + grid_static_v1 unit tests (Phase 3.8a)."""

from __future__ import annotations

import pytest

from trading.engine.continuous_strategy_base import (
    Cancel,
    CancelAll,
    Place,
    Reset,
)
from trading.paper.driver_continuous import ContinuousDriver
from trading.paper.limit_book_sim import LimitBookSim, LimitFill
from trading.strategies.grid.grid_base import compute_levels
from trading.strategies.grid.grid_static_v1 import GridStaticV1


def _cfg(**overrides) -> dict:
    base = {
        "strategy_id": "grid_test",
        "instrument_id": "BTCUSDT.BINANCE",
        "params": {
            "step": 100.0,
            "n_below": 3,
            "n_above": 3,
            "qty_per_level": 0.01,
            "stop_loss_pct": 0.15,
            "geometric": False,
        },
    }
    base["params"].update(overrides)
    return base


def test_compute_levels_arithmetic() -> None:
    levels = compute_levels(center=1000.0, step=10.0, n_below=3, n_above=3)
    prices_buy = sorted(lvl.price for lvl in levels if lvl.side == "BUY")
    prices_sell = sorted(lvl.price for lvl in levels if lvl.side == "SELL")
    assert prices_buy == [970.0, 980.0, 990.0]
    assert prices_sell == [1010.0, 1020.0, 1030.0]


def test_compute_levels_geometric() -> None:
    levels = compute_levels(center=100.0, step=0.01, n_below=2, n_above=2, geometric=True)
    by_side = {
        ("BUY", -1): 99.0,
        ("BUY", -2): 99.0 * 0.99,
        ("SELL", 1): 101.0,
        ("SELL", 2): 101.0 * 1.01,
    }
    for lvl in levels:
        expected = by_side[(lvl.side, lvl.idx)]
        assert pytest.approx(lvl.price, rel=1e-9) == expected


def test_compute_levels_rejects_zero_center() -> None:
    with pytest.raises(ValueError):
        compute_levels(center=0.0, step=1.0, n_below=1, n_above=1)


def test_compute_levels_skips_negative_buy_prices() -> None:
    # center=10, step=5, n_below=3 → prices 5, 0, -5 — only 5 kept.
    levels = compute_levels(center=10.0, step=5.0, n_below=3, n_above=1)
    buys = [lvl for lvl in levels if lvl.side == "BUY"]
    assert [lvl.price for lvl in buys] == [5.0]


def test_static_v1_on_start_buy_only_default() -> None:
    """3.8a.1: default buy_only=True places only BUYs at start; SELLs are
    emitted by on_fill (paired-mirror)."""
    strat = GridStaticV1(_cfg())
    actions = strat.on_start(spot_px=75000.0, ts=1.0)
    placed = [a for a in actions if isinstance(a, Place)]
    assert len(placed) == 3  # 3 BUYs below, no SELLs at start
    assert {a.order.side for a in placed} == {"BUY"}
    buy_prices = sorted(a.order.price for a in placed)
    assert buy_prices == [74700.0, 74800.0, 74900.0]


def test_static_v1_on_start_symmetric_when_buy_only_false() -> None:
    """Legacy symmetric mode remains available via buy_only=False."""
    strat = GridStaticV1(_cfg(buy_only=False))
    actions = strat.on_start(spot_px=75000.0, ts=1.0)
    placed = [a for a in actions if isinstance(a, Place)]
    assert len(placed) == 6  # 3 BUYs + 3 SELLs
    assert {a.order.side for a in placed} == {"BUY", "SELL"}


def test_static_v1_deterministic_coids() -> None:
    s1 = GridStaticV1(_cfg()).on_start(spot_px=75000.0, ts=1.0)
    s2 = GridStaticV1(_cfg()).on_start(spot_px=75000.0, ts=2.0)
    coids_1 = {a.order.coid for a in s1}
    coids_2 = {a.order.coid for a in s2}
    # Same params + center → same coids regardless of ts.
    assert coids_1 == coids_2


def test_static_v1_stop_loss_cancels_all() -> None:
    strat = GridStaticV1(_cfg())
    strat.on_start(spot_px=1000.0, ts=1.0)
    # Drop 14.9% — still above floor.
    actions = strat.on_trade_tick(px=851.0, ts=2.0)
    assert actions == []
    # Drop 15% — triggers stop-loss.
    actions = strat.on_trade_tick(px=850.0, ts=3.0)
    assert len(actions) == 1 and isinstance(actions[0], CancelAll)
    assert "stop_loss" in actions[0].reason
    assert strat.state.stopped_out is True
    # Subsequent ticks don't re-trigger.
    assert strat.on_trade_tick(px=840.0, ts=4.0) == []


def test_static_v1_buy_fill_posts_paired_sell() -> None:
    """3.8a.1 buy_only flow: BUY fill at level -1 triggers a paired
    SELL at level +1 at the mirrored price."""
    strat = GridStaticV1(_cfg())
    strat.on_start(spot_px=1000.0, ts=1.0)
    fill = LimitFill(
        coid="buy-fill-coid",
        strategy_id=strat.strategy_id,
        instrument_id=strat.instrument_id,
        side="BUY",
        price=900.0,  # level -1 when center=1000, step=100
        qty=0.01,
        ts=2.0,
        fee=0.0,
    )
    actions = strat.on_fill(fill)
    assert len(actions) == 1 and isinstance(actions[0], Place)
    order = actions[0].order
    assert order.side == "SELL"
    assert order.price == 1100.0  # center + 1 * step
    # Second fill at same level should NOT double-post (open_sells_by_level).
    fill2 = LimitFill(
        coid="buy-fill2-coid",
        strategy_id=strat.strategy_id,
        instrument_id=strat.instrument_id,
        side="BUY",
        price=900.0,
        qty=0.01,
        ts=3.0,
        fee=0.0,
    )
    assert strat.on_fill(fill2) == []


def test_static_v1_symmetric_fill_is_bookkeeping_only() -> None:
    """Symmetric mode (buy_only=False): fill consumes one leg and the
    opposite leg is already in the book from on_start — no new actions."""
    strat = GridStaticV1(_cfg(buy_only=False))
    strat.on_start(spot_px=1000.0, ts=1.0)
    fill = LimitFill(
        coid="doesnt-matter",
        strategy_id=strat.strategy_id,
        instrument_id=strat.instrument_id,
        side="BUY",
        price=900.0,
        qty=0.01,
        ts=2.0,
        fee=0.0,
    )
    assert strat.on_fill(fill) == []
    assert strat.state.last_fill_by_level[-1] == "BUY"


async def test_driver_start_applies_initial_actions_buy_only() -> None:
    strat = GridStaticV1(_cfg())
    book = LimitBookSim(persist=False)
    driver = ContinuousDriver(strategy=strat, book=book)
    await driver.start(spot_px=1000.0, ts=1.0)
    assert len(book) == 3
    assert driver.stats.placed == 3


async def test_driver_buy_fill_posts_paired_sell_end_to_end() -> None:
    strat = GridStaticV1(_cfg())
    book = LimitBookSim(persist=False)
    driver = ContinuousDriver(strategy=strat, book=book)
    await driver.start(spot_px=1000.0, ts=1.0)
    # Book: 3 BUYs at 900, 800, 700. Tick to 900 → one BUY fills,
    # paired SELL at 1100 is placed.
    await driver.on_tick(px=900.0, ts=2.0)
    assert driver.stats.fills == 1
    # 2 remaining BUYs + 1 new SELL = 3.
    assert len(book) == 3
    sides = {o.side for o in book.snapshot()}
    assert sides == {"BUY", "SELL"}


async def test_driver_applies_cancel_action() -> None:
    strat = GridStaticV1(_cfg())
    book = LimitBookSim(persist=False)
    driver = ContinuousDriver(strategy=strat, book=book)
    await driver.start(spot_px=1000.0, ts=1.0)
    # Book has 3 BUYs from buy_only start. Cancel one.
    any_coid = next(iter(book.snapshot())).coid
    await driver._apply([Cancel(coid=any_coid, reason="test")], ts=2.0)
    assert len(book) == 2
    assert driver.stats.cancelled == 1


async def test_driver_reset_cancels_all_and_bumps_gen() -> None:
    strat = GridStaticV1(_cfg())
    book = LimitBookSim(persist=False)
    driver = ContinuousDriver(strategy=strat, book=book)
    await driver.start(spot_px=1000.0, ts=1.0)
    await driver._apply([Reset(new_center=1050.0)], ts=2.0)
    assert len(book) == 0
    assert strat.state.reset_gen == 1
    assert strat.state.center_price == 1050.0
    assert driver.stats.resets == 1


async def test_driver_stop_cancels_all() -> None:
    strat = GridStaticV1(_cfg())
    book = LimitBookSim(persist=False)
    driver = ContinuousDriver(strategy=strat, book=book)
    await driver.start(spot_px=1000.0, ts=1.0)
    await driver.stop(ts=10.0)
    assert len(book) == 0
