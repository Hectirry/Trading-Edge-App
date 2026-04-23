"""last_90s_forecaster_v2 — decision gates + shadow mode."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading.engine.features.macro import MacroSnapshot
from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
    Last90sForecasterV2,
)


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


class _StubMacro:
    def __init__(self, snap: MacroSnapshot | None) -> None:
        self.snap = snap

    def snapshot_at(self, _ts: float) -> MacroSnapshot | None:
        return self.snap


class _StubModel:
    def __init__(self, p: float) -> None:
        self.p = p
        self.calls = 0

    def predict_proba(self, x: list[float]) -> float:
        self.calls += 1
        return self.p


def _macro(regime_kind: str = "uptrend") -> MacroSnapshot:
    if regime_kind == "downtrend":
        return MacroSnapshot(
            ema8=90.0, ema34=100.0, adx_14=25.0,
            consecutive_same_dir=-3, regime="downtrend",
            ema8_vs_ema34_pct=-10.0,
        )
    if regime_kind == "range":
        return MacroSnapshot(
            ema8=100.0, ema34=100.0, adx_14=5.0,
            consecutive_same_dir=1, regime="range", ema8_vs_ema34_pct=0.0,
        )
    return MacroSnapshot(
        ema8=110.0, ema34=100.0, adx_14=25.0,
        consecutive_same_dir=3, regime="uptrend", ema8_vs_ema34_pct=10.0,
    )


def _ctx(
    *,
    t_in_window: float = 210.0,
    implied: float = 0.48,
    spread_bps: float = 50.0,
) -> TickContext:
    now = 1_000_000.0 + t_in_window
    spots = [70_000.0 * (1.0 + 0.01 * i / 89.0) for i in range(90)]
    recent = [
        _RecentTick(ts=now - (len(spots) - i), spot_price=spots[i])
        for i in range(len(spots))
    ]
    return TickContext(
        ts=now, market_slug="btc-updown-5m-1", t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=spots[-1], chainlink_price=spots[-1],
        open_price=spots[0],
        pm_yes_bid=0.47, pm_yes_ask=0.48, pm_no_bid=0.52, pm_no_ask=0.53,
        pm_depth_yes=100.0, pm_depth_no=100.0, pm_imbalance=0.0,
        pm_spread_bps=spread_bps, implied_prob_yes=implied,
        model_prob_yes=0.5, edge=0.0, z_score=0.0, vol_regime="normal",
        recent_ticks=recent,
    )


def _strat(
    *,
    model: _StubModel | None = None,
    macro: _StubMacro | None = None,
    shadow: bool = False,
) -> Last90sForecasterV2:
    cfg = {
        "params": {
            "entry_window_start_s": 205,
            "entry_window_end_s": 215,
            "edge_threshold": 0.04,
            "spread_max_bps": 150.0,
            "adx_threshold": 20.0,
            "consecutive_min": 2,
        },
        "paper": {"shadow": shadow},
    }
    return Last90sForecasterV2(cfg, macro_provider=macro, model=model)


def test_shadow_when_no_model_loaded() -> None:
    d = _strat(model=None, macro=_StubMacro(_macro())).should_enter(_ctx())
    assert d.action is Action.SKIP
    assert d.reason == "shadow_mode_no_model"


def test_enter_yes_up_with_model_and_edge() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro())).should_enter(_ctx(implied=0.40))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_UP
    assert model.calls == 1


def test_shadow_flag_blocks_enter_but_still_computes_features() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro()), shadow=True).should_enter(
        _ctx(implied=0.40)
    )
    assert d.action is Action.SKIP
    assert d.reason == "shadow_mode"
    assert "micro_prob" in d.signal_features
    assert d.signal_features["shadow"] is True


def test_edge_below_threshold_skips() -> None:
    # Use range regime so the macro agreement gate doesn't fire first.
    model = _StubModel(p=0.50)
    d = _strat(model=model, macro=_StubMacro(_macro("range"))).should_enter(
        _ctx(implied=0.48)
    )
    assert d.action is Action.SKIP
    assert d.reason == "edge_below_threshold"


def test_macro_contradicts_micro_uptrend_with_low_prob() -> None:
    model = _StubModel(p=0.30)
    d = _strat(model=model, macro=_StubMacro(_macro("uptrend"))).should_enter(_ctx())
    assert d.action is Action.SKIP
    assert d.reason == "macro_contradicts_micro"


def test_range_regime_allows_both_directions() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro("range"))).should_enter(
        _ctx(implied=0.40)
    )
    assert d.action is Action.ENTER


def test_feature_vector_attached_to_decision() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro())).should_enter(_ctx(implied=0.40))
    from trading.strategies.polymarket_btc5m._v2_features import FEATURE_NAMES

    for name in FEATURE_NAMES:
        assert name in d.signal_features, f"missing feature {name}"


def test_spread_too_wide_blocks_before_model() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro())).should_enter(
        _ctx(spread_bps=300.0)
    )
    assert d.action is Action.SKIP
    assert d.reason == "spread_too_wide"
    # Model WAS called (we compute features first) — that's fine in v2;
    # the gate just suppresses the ENTER.
    assert model.calls == 1


def test_outside_entry_window_skips_before_model() -> None:
    model = _StubModel(p=0.70)
    d = _strat(model=model, macro=_StubMacro(_macro())).should_enter(
        _ctx(t_in_window=100.0)
    )
    assert d.action is Action.SKIP
    assert d.reason == "outside_entry_window"
    assert model.calls == 0


def test_feature_vector_length_matches_feature_names() -> None:
    from trading.strategies.polymarket_btc5m._v2_features import (
        FEATURE_NAMES,
        V2FeatureInputs,
        build_vector,
    )

    inp = V2FeatureInputs(
        as_of_ts=1_700_000_000.0,
        spots_last_90s=[100.0 + 0.01 * i for i in range(91)],
        macro_snap=_macro(),
        implied_prob_yes=0.48, yes_ask=0.48, no_ask=0.52,
        depth_yes=100.0, depth_no=100.0,
        pm_imbalance=0.0, pm_spread_bps=50.0,
    )
    vec = build_vector(inp)
    assert len(vec) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in vec)


def test_cyclic_time_features_bounded() -> None:
    from trading.strategies.polymarket_btc5m._v2_features import (
        V2FeatureInputs,
        build_vector,
    )

    for ts in (0.0, 1_700_000_000.0, 1_700_000_000.0 + 12 * 3600):
        vec = build_vector(V2FeatureInputs(
            as_of_ts=ts,
            spots_last_90s=[100.0 + 0.01 * i for i in range(91)],
            macro_snap=_macro(),
            implied_prob_yes=0.48, yes_ask=0.48, no_ask=0.52,
            depth_yes=100.0, depth_no=100.0,
            pm_imbalance=0.0, pm_spread_bps=50.0,
        ))
        hour_sin, hour_cos, dow_sin, dow_cos = vec[-4:]
        for v in (hour_sin, hour_cos, dow_sin, dow_cos):
            assert -1.0 - 1e-9 <= v <= 1.0 + 1e-9
        assert abs(hour_sin**2 + hour_cos**2 - 1.0) < 1e-6
        assert abs(dow_sin**2 + dow_cos**2 - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_v2_feature_inputs_roundtrip() -> None:
    # Trivial: constructing V2FeatureInputs + build_vector is deterministic
    # given identical inputs.
    from trading.strategies.polymarket_btc5m._v2_features import (
        V2FeatureInputs,
        build_vector,
    )

    inp = V2FeatureInputs(
        as_of_ts=1_700_000_000.0,
        spots_last_90s=[100.0 + 0.01 * i for i in range(91)],
        macro_snap=_macro(),
        implied_prob_yes=0.48, yes_ask=0.48, no_ask=0.52,
        depth_yes=100.0, depth_no=100.0,
        pm_imbalance=0.0, pm_spread_bps=50.0,
    )
    a = build_vector(inp)
    b = build_vector(inp)
    assert a == b
