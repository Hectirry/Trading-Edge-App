"""last_90s_forecaster_v1 decision tree — every SKIP + ENTER leaf."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading.engine.features.macro import MacroSnapshot
from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v1 import (
    Last90sForecasterV1,
)


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


def _macro(
    ema_fast: float = 110.0,
    ema_slow: float = 100.0,
    adx: float = 25.0,
    consec: int = 3,
) -> MacroSnapshot:
    return MacroSnapshot(
        ema8=ema_fast,
        ema34=ema_slow,
        adx_14=adx,
        consecutive_same_dir=consec,
        regime="uptrend",  # reclassified by strategy
        ema8_vs_ema34_pct=(ema_fast - ema_slow) / ema_slow * 100.0,
    )


class _StubMacro:
    def __init__(self, snap: MacroSnapshot | None) -> None:
        self.snap = snap

    def snapshot_at(self, _ts: float) -> MacroSnapshot | None:
        return self.snap


def _ctx(
    *,
    t_in_window: float = 210.0,
    implied: float = 0.48,
    spread_bps: float = 50.0,
    spots: list[float] | None = None,
) -> TickContext:
    now = 1_000_000.0 + t_in_window
    if spots is None:
        # 90 strictly-rising samples. Linear from 70_000 → 70_700 = +100 bps
        # exactly, so momentum_bps(_, 90) ≈ 100 and micro_prob saturates at 0.95.
        spots = [70_000.0 * (1.0 + 0.01 * i / 89.0) for i in range(90)]
    recent = [
        _RecentTick(ts=now - (len(spots) - i), spot_price=spots[i])
        for i in range(len(spots))
    ]
    spot_now = spots[-1]
    return TickContext(
        ts=now,
        market_slug="btc-updown-5m-1",
        t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=spot_now,
        chainlink_price=spot_now,
        open_price=spots[0],
        pm_yes_bid=0.47, pm_yes_ask=0.48, pm_no_bid=0.52, pm_no_ask=0.53,
        pm_depth_yes=100.0, pm_depth_no=100.0,
        pm_imbalance=0.0,
        pm_spread_bps=spread_bps,
        implied_prob_yes=implied,
        model_prob_yes=0.5, edge=0.0, z_score=0.0, vol_regime="normal",
        recent_ticks=recent,
    )


def _strat(params_overrides: dict | None = None, macro=None) -> Last90sForecasterV1:
    params = {
        "entry_window_start_s": 205,
        "entry_window_end_s": 215,
        "momentum_divisor_bps": 40.0,
        "edge_threshold": 0.04,
        "spread_max_bps": 150.0,
        "adx_threshold": 20.0,
        "consecutive_min": 2,
    }
    params.update(params_overrides or {})
    return Last90sForecasterV1({"params": params}, macro_provider=macro)


def test_skip_outside_entry_window_before() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(t_in_window=180.0))
    assert d.action is Action.SKIP
    assert d.reason == "outside_entry_window"


def test_skip_outside_entry_window_after() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(t_in_window=230.0))
    assert d.action is Action.SKIP
    assert d.reason == "outside_entry_window"


def test_skip_insufficient_micro_data() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(
        _ctx(spots=[70_000.0] * 30)
    )
    assert d.action is Action.SKIP
    assert d.reason == "insufficient_micro_data"


def test_skip_no_macro_snapshot() -> None:
    d = _strat(macro=_StubMacro(None)).should_enter(_ctx())
    assert d.action is Action.SKIP
    assert d.reason == "no_macro_snapshot"


def test_skip_spread_too_wide() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(spread_bps=200.0))
    assert d.action is Action.SKIP
    assert d.reason == "spread_too_wide"


def test_skip_macro_contradicts_micro_uptrend_falling() -> None:
    # Micro: BTC falling → micro_prob < 0.5; regime=uptrend → disagree.
    spots = [70_000.0 * (1.0 - 0.01 * i / 89.0) for i in range(90)]
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(spots=spots))
    assert d.action is Action.SKIP
    assert d.reason == "macro_contradicts_micro"


def test_skip_macro_contradicts_micro_downtrend_rising() -> None:
    # Macro downtrend + micro up.
    macro = MacroSnapshot(
        ema8=90.0, ema34=100.0, adx_14=25.0,
        consecutive_same_dir=-3, regime="downtrend", ema8_vs_ema34_pct=-10.0,
    )
    d = _strat(macro=_StubMacro(macro)).should_enter(_ctx())  # spots rising
    assert d.action is Action.SKIP
    assert d.reason == "macro_contradicts_micro"


def test_skip_edge_below_threshold_when_implied_matches() -> None:
    # micro_prob ≈ 0.95 (momentum +100 bps clamped), so edge ≈ 0.47; match implied.
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(implied=0.95))
    assert d.action is Action.SKIP
    assert d.reason == "edge_below_threshold"


def test_enter_yes_up_when_edge_positive() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(implied=0.40))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_UP
    assert d.signal_features["edge"] > 0


def test_enter_yes_down_when_edge_negative() -> None:
    # Falling micro + downtrend macro → negative edge.
    spots = [70_000.0 * (1.0 - 0.01 * i / 89.0) for i in range(90)]
    macro = MacroSnapshot(
        ema8=90.0, ema34=100.0, adx_14=25.0,
        consecutive_same_dir=-3, regime="downtrend", ema8_vs_ema34_pct=-10.0,
    )
    d = _strat(macro=_StubMacro(macro)).should_enter(
        _ctx(spots=spots, implied=0.60)
    )
    assert d.action is Action.ENTER
    assert d.side is Side.YES_DOWN
    assert d.signal_features["edge"] < 0


def test_range_regime_allows_both_sides() -> None:
    # Weak ADX → regime classifier returns "range" → agreement = True.
    macro = MacroSnapshot(
        ema8=100.0, ema34=100.0, adx_14=5.0,
        consecutive_same_dir=1, regime="range", ema8_vs_ema34_pct=0.0,
    )
    d = _strat(macro=_StubMacro(macro)).should_enter(_ctx(implied=0.40))
    assert d.action is Action.ENTER


def test_features_surface_in_decision() -> None:
    d = _strat(macro=_StubMacro(_macro())).should_enter(_ctx(implied=0.40))
    for k in ("m30_bps", "m60_bps", "m90_bps", "rv_90s", "tick_up_ratio",
              "micro_prob", "edge", "regime", "ema8", "ema34", "adx_14",
              "consecutive_same_dir", "implied_prob_yes", "pm_spread_bps"):
        assert k in d.signal_features, k


def test_no_time_leak_future_ticks_ignored() -> None:
    # Add future ticks beyond ctx.ts → micro should still only use last 90 s
    # up to ctx.ts. We simulate by appending ticks with ts > ctx.ts.
    ctx = _ctx()
    future_tick = _RecentTick(ts=ctx.ts + 30, spot_price=999_999.0)
    ctx.recent_ticks = list(ctx.recent_ticks) + [future_tick]
    d = _strat(macro=_StubMacro(_macro())).should_enter(ctx)
    # Would have been an ENTER; the future tick must not affect momentum.
    # If we leaked future data, momentum would spike and edge jumps.
    assert d.signal_features["m90_bps"] == pytest.approx(100.0, abs=2.0)
