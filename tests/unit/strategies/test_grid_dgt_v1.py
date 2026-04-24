"""grid_dgt_v1 unit tests (Phase 3.8a)."""

from __future__ import annotations

from trading.engine.continuous_strategy_base import Place, Reset
from trading.paper.driver_continuous import ContinuousDriver
from trading.paper.limit_book_sim import LimitBookSim
from trading.strategies.grid.grid_dgt_v1 import GridDgtV1


def _cfg(**overrides) -> dict:
    base = {
        "strategy_id": "dgt_test",
        "instrument_id": "BTCUSDT.BINANCE",
        "params": {
            "step": 100.0,
            "n_below": 3,
            "n_above": 3,
            "qty_per_level": 0.01,
            "stop_loss_pct": 0.50,  # keep out of the way for breach tests
            "geometric": False,
        },
    }
    base["params"].update(overrides)
    return base


def test_dgt_inside_range_is_noop() -> None:
    s = GridDgtV1(_cfg())
    s.on_start(spot_px=1000.0, ts=1.0)
    # Inside [700, 1300] — nothing to do.
    assert s.on_trade_tick(px=1050.0, ts=2.0) == []
    assert s.on_trade_tick(px=900.0, ts=3.0) == []


def test_dgt_upper_breach_emits_reset_then_grid() -> None:
    s = GridDgtV1(_cfg())
    s.on_start(spot_px=1000.0, ts=1.0)
    # Break above 1300 (= center + 3*step).
    actions = s.on_trade_tick(px=1301.0, ts=2.0)
    assert isinstance(actions[0], Reset)
    assert actions[0].new_center == 1301.0
    assert "upper" in actions[0].reason
    places = [a for a in actions if isinstance(a, Place)]
    # 3.8a.1 default buy_only=True: rebuild places only 3 BUYs
    assert len(places) == 3
    assert {p.order.side for p in places} == {"BUY"}


def test_dgt_lower_breach_emits_reset_with_lower_reason() -> None:
    s = GridDgtV1(_cfg())
    s.on_start(spot_px=1000.0, ts=1.0)
    actions = s.on_trade_tick(px=650.0, ts=2.0)
    assert isinstance(actions[0], Reset)
    assert "lower" in actions[0].reason


def test_dgt_reset_place_uses_next_gen_coids() -> None:
    """Driver bumps reset_gen on Reset; the Place actions in the same batch
    must embed the post-bump gen so the book doesn't duplicate-reject."""
    s = GridDgtV1(_cfg())
    s.on_start(spot_px=1000.0, ts=1.0)
    gen_0_sample = s._place_grid_actions(center=1301.0, ts=2.0, reset_gen=0)
    actions = s.on_trade_tick(px=1301.0, ts=2.0)
    places = [a for a in actions if isinstance(a, Place)]
    coids_new = {a.order.coid for a in places}
    coids_if_stale = {a.order.coid for a in gen_0_sample}
    # The DGT code uses reset_gen=next_gen=1; if it used stale gen=0,
    # the coid sets would match — they must not.
    assert coids_new.isdisjoint(coids_if_stale)


async def test_dgt_driver_reset_cancels_only_buys_in_buy_only_mode() -> None:
    """3.8a.2 regression: Reset action triggered via strategy cancels only
    BUYs in buy_only mode; any resting SELL survives. Uses an artificially
    placed SELL outside the tick-fill path so we isolate the cancel-all
    filtering behaviour."""
    from trading.engine.continuous_strategy_base import Reset
    from trading.paper.limit_book_sim import LimitOrder

    s = GridDgtV1(_cfg())
    book = LimitBookSim(persist=False)
    drv = ContinuousDriver(strategy=s, book=book)
    await drv.start(spot_px=1000.0, ts=1.0)
    assert len(book) == 3  # 3 BUYs

    # Inject a SELL far above the grid — won't fill at the Reset tick.
    await book.place(
        LimitOrder(
            coid="paired-sell-surv",
            strategy_id=s.strategy_id,
            instrument_id=s.instrument_id,
            side="SELL",
            price=9999.0,
            qty=0.01,
            ts_placed=1.5,
        )
    )
    assert len(book) == 4

    # Apply Reset action directly (bypass on_trade_tick → no book.on_tick fills).
    await drv._apply([Reset(new_center=1200.0, reason="test")], ts=2.0)
    sides = [o.side for o in book.snapshot()]
    assert sides.count("SELL") == 1  # preserved
    assert sides.count("BUY") == 0  # all cancelled (new BUYs will come from next tick)


async def test_dgt_driver_end_to_end_reset_and_place() -> None:
    s = GridDgtV1(_cfg())
    book = LimitBookSim(persist=False)
    drv = ContinuousDriver(strategy=s, book=book)
    await drv.start(spot_px=1000.0, ts=1.0)
    # 3.8a.1 default buy_only=True → 3 BUYs at start.
    assert len(book) == 3 and s.state.reset_gen == 0
    # Breach upper.
    await drv.on_tick(px=1301.0, ts=2.0)
    assert s.state.reset_gen == 1
    assert s.state.center_price == 1301.0
    # Old grid cancelled, new 3-BUY grid placed around 1301.
    assert len(book) == 3
    assert drv.stats.resets == 1


async def test_dgt_stop_loss_beats_breach_check() -> None:
    """If stop-loss floor is hit first, no reset fires."""
    s = GridDgtV1(_cfg(stop_loss_pct=0.05))
    book = LimitBookSim(persist=False)
    drv = ContinuousDriver(strategy=s, book=book)
    await drv.start(spot_px=1000.0, ts=1.0)
    # -6% from center: stop-loss (at 950) triggers; breach check never runs.
    await drv.on_tick(px=940.0, ts=2.0)
    assert s.state.stopped_out is True
    assert drv.stats.resets == 0
    assert len(book) == 0


def test_dgt_stopped_out_is_terminal() -> None:
    s = GridDgtV1(_cfg(stop_loss_pct=0.05))
    s.on_start(spot_px=1000.0, ts=1.0)
    # First tick triggers stop-loss.
    s.on_trade_tick(px=940.0, ts=2.0)
    assert s.state.stopped_out is True
    # Subsequent breach-worthy tick emits nothing.
    assert s.on_trade_tick(px=2000.0, ts=3.0) == []
