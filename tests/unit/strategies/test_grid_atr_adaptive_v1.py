"""grid_atr_adaptive_v1 unit tests (Phase 3.8a)."""

from __future__ import annotations

from trading.engine.continuous_strategy_base import Bar, Place, Reset
from trading.paper.driver_continuous import ContinuousDriver
from trading.paper.limit_book_sim import LimitBookSim
from trading.strategies.grid.grid_atr_adaptive_v1 import GridAtrAdaptiveV1


def _cfg(**overrides) -> dict:
    base = {
        "strategy_id": "atr_test",
        "instrument_id": "BTCUSDT.BINANCE",
        "params": {
            "step": 100.0,
            "n_below": 3,
            "n_above": 3,
            "qty_per_level": 0.01,
            "stop_loss_pct": 0.50,
            "atr_period": 3,
            "atr_multiplier": 1.0,
            "recompute_delta_pct": 0.20,
            # Tests feed 1m bars directly — bypass 15m aggregation.
            "atr_bar_window_s": 60.0,
            # Disable cooldown to let tests drive tight sequences.
            "rebuild_cooldown_s": 0.0,
        },
    }
    base["params"].update(overrides)
    return base


def _bar(ts: float, hlc: tuple[float, float, float]) -> Bar:
    h, lo, c = hlc
    return Bar(ts_open=ts - 60, ts_close=ts, open=c, high=h, low=lo, close=c, volume=0.0)


def test_atr_bar_pre_seed_is_noop() -> None:
    s = GridAtrAdaptiveV1(_cfg(atr_period=5))
    s.on_start(spot_px=1000.0, ts=1.0)
    # Fewer than period bars → ATR not ready, no rebuild.
    assert s.on_bar_1m(_bar(60.0, (1005.0, 995.0, 1000.0))) == []
    assert s.on_bar_1m(_bar(120.0, (1006.0, 994.0, 1000.0))) == []
    assert s.state.reset_gen == 0


def test_atr_small_delta_does_not_rebuild() -> None:
    s = GridAtrAdaptiveV1(_cfg(atr_multiplier=1.0, recompute_delta_pct=0.5))
    s.on_start(spot_px=1000.0, ts=1.0)
    # Constant range TR=10 → new_step=10; old step=100 → delta=0.9>0.5 → rebuilds
    # on the seed bar. Subsequent bars keep TR=10; step stable → no rebuild.
    actions_per_bar = [s.on_bar_1m(_bar(60 * (i + 1), (1005.0, 995.0, 1000.0))) for i in range(10)]
    rebuilds = [a for a in actions_per_bar if a]
    assert len(rebuilds) == 1  # exactly one rebuild across 10 bars


def test_atr_rebuild_emits_reset_and_full_grid() -> None:
    s = GridAtrAdaptiveV1(_cfg(recompute_delta_pct=0.20))
    s.on_start(spot_px=1000.0, ts=1.0)
    # Feed period bars with TR=20 → ATR=20 → new_step=20, large delta vs 100.
    actions_seq = []
    for i in range(5):
        actions_seq.append(s.on_bar_1m(_bar(60 * (i + 1), (1010.0, 990.0, 1000.0))))
    rebuild = next(a for a in actions_seq if a)
    assert isinstance(rebuild[0], Reset)
    assert rebuild[0].reason == "atr_shift"
    assert rebuild[0].new_center == 1000.0
    places = [a for a in rebuild if isinstance(a, Place)]
    # 3.8a.1 default buy_only=True → only 3 BUYs (not 6).
    assert len(places) == 3
    assert {p.order.side for p in places} == {"BUY"}


def test_atr_expansion_rebuilds_again() -> None:
    s = GridAtrAdaptiveV1(_cfg(recompute_delta_pct=0.20))
    s.on_start(spot_px=1000.0, ts=1.0)
    phase1 = [s.on_bar_1m(_bar(60 * (i + 1), (1010.0, 990.0, 1000.0))) for i in range(5)]
    rebuilds_phase1 = [a for a in phase1 if a]
    # Expansion — TR jumps to 60.
    phase2 = [s.on_bar_1m(_bar(60 * (5 + i + 1), (1030.0, 970.0, 1000.0))) for i in range(10)]
    rebuilds_phase2 = [a for a in phase2 if a]
    assert len(rebuilds_phase2) >= 1
    assert len(rebuilds_phase1) >= 1  # sanity — initial rebuild fired


async def test_atr_driver_rebuild_cancels_old_grid() -> None:
    s = GridAtrAdaptiveV1(_cfg(recompute_delta_pct=0.20))
    book = LimitBookSim(persist=False)
    drv = ContinuousDriver(strategy=s, book=book)
    await drv.start(spot_px=1000.0, ts=1.0)
    # 3.8a.1 buy_only default → 3 BUYs at start.
    assert len(book) == 3
    for i in range(5):
        await drv.on_bar_1m(_bar(60 * (i + 1), (1010.0, 990.0, 1000.0)))
    # After rebuild: 3 new BUYs, old ones cancelled.
    assert len(book) == 3
    assert drv.stats.resets >= 1
    assert 0 < s.step < 100.0


def test_atr_multiplier_scales_step() -> None:
    s = GridAtrAdaptiveV1(_cfg(atr_multiplier=2.0))
    s.on_start(spot_px=1000.0, ts=1.0)
    for i in range(5):
        s.on_bar_1m(_bar(60 * (i + 1), (1010.0, 990.0, 1000.0)))
    # ATR ≈ 20, multiplier=2 → step ≈ 40 (not the default 20 at multiplier=1).
    assert abs(s.step - 40.0) < 5.0
