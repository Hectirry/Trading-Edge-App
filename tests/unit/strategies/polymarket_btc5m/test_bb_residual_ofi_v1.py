"""bb_residual_ofi_v1 — gates, shadow, fee convexity, alpha clamp."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.bb_residual_ofi_v1 import (
    BBResidualOFIV1,
    FEATURE_NAMES,
    _alpha_shrinkage,
    _convex_fee,
)


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


class _StubModel:
    """Fixed p_edge — lets us isolate the gate logic from the model."""

    def __init__(self, p: float) -> None:
        self.p = p
        self.calls = 0

    def predict_proba(self, x: list[float]) -> float:
        self.calls += 1
        return self.p


class _StubMS:
    def __init__(self, **overrides: float) -> None:
        self._d = {
            "bm_cvd_normalized": 0.0,
            "bm_taker_buy_ratio": 0.5,
            "bm_trade_intensity": 1.0,
            "bm_large_trade_flag": 0.0,
            "bm_signed_autocorr_lag1": 0.0,
        }
        self._d.update(overrides)

    def fetch(self, ts: float) -> dict[str, float]:
        return dict(self._d)


def _ctx(
    *,
    t_in_window: float = 195.0,
    open_price: float = 77_540.0,
    spot_drift_bps: float = 17.0,
    implied: float = 0.65,
    spread_bps: float = 50.0,
    market_slug: str = "btc-updown-5m-1",
) -> TickContext:
    """A 105 s tail of 1 Hz spots that drifts ``spot_drift_bps`` over
    the window — enough to push p_BM well above 0.5 at t=195."""
    now = 1_700_000_000.0 + t_in_window
    n = 105
    drift_per_step = spot_drift_bps * 1e-4 / n
    spots = [open_price * (1.0 + drift_per_step * i) for i in range(n)]
    recent = [
        _RecentTick(ts=now - (n - i), spot_price=spots[i]) for i in range(n)
    ]
    return TickContext(
        ts=now,
        market_slug=market_slug,
        t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=spots[-1],
        chainlink_price=spots[-1],
        open_price=open_price,
        pm_yes_bid=implied - 0.005,
        pm_yes_ask=implied + 0.005,
        pm_no_bid=(1 - implied) - 0.005,
        pm_no_ask=(1 - implied) + 0.005,
        pm_depth_yes=100.0,
        pm_depth_no=100.0,
        pm_imbalance=0.0,
        pm_spread_bps=spread_bps,
        implied_prob_yes=implied,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=0.0,
        vol_regime="normal",
        recent_ticks=recent,
    )


def _strat(
    *,
    model: _StubModel | None = None,
    ms: _StubMS | None = None,
    shadow: bool = False,
    overrides: dict | None = None,
) -> BBResidualOFIV1:
    params = {
        "entry_window_start_s": 60,
        "entry_window_end_s": 290,
        "fee_k": 0.0315,
        "bb_T_seconds": 300.0,
        "microstructure_window_seconds": 30,
        "large_trade_threshold_usd": 100000.0,
        "ofi_coinbase_weight": 0.0,
        "sharpe_threshold": 2.0,
        "sharpe_threshold_late": 1.5,
        "sharpe_late_t_to_close_s": 30.0,
        "edge_net_min": 0.0,
        # 0.005 makes Sharpe = edge_net / 0.005, so a 0.04 edge_net
        # gives Sharpe = 8.0 — comfortably above the 2.0 gate without
        # the test having to simulate a real ensemble.
        "p_edge_sigma": 0.005,
        "alpha_min": 0.4,
        "alpha_max": 0.85,
        "alpha_ofi_gain": 1.0,
        "alpha_large_trade_bonus": 0.1,
        "spread_max_bps": 300.0,
    }
    if overrides:
        params.update(overrides)
    cfg = {"params": params, "paper": {"shadow": shadow}}
    return BBResidualOFIV1(cfg, model=model, microstructure_provider=ms)


# ---------------- pure helpers ---------------- #


def test_convex_fee_peaks_at_half() -> None:
    """fee(0.5) = fee_k; corners are zero."""
    assert _convex_fee(0.5, 0.0315) == pytest.approx(0.0315, abs=1e-12)
    assert _convex_fee(0.0, 0.0315) == 0.0
    assert _convex_fee(1.0, 0.0315) == 0.0
    # Symmetry around 0.5.
    for p in (0.1, 0.3, 0.7, 0.9):
        assert _convex_fee(p, 0.0315) == pytest.approx(_convex_fee(1 - p, 0.0315))


def test_convex_fee_clamps_out_of_range() -> None:
    assert _convex_fee(-0.1, 0.0315) == 0.0
    assert _convex_fee(1.1, 0.0315) == 0.0


def test_alpha_clamps_within_bounds() -> None:
    a_low = _alpha_shrinkage(
        ofi_abs=0.0,
        large_trade_flag=0.0,
        t_in_window_s=60.0,
        entry_start_s=60,
        entry_end_s=290,
        alpha_min=0.4,
        alpha_max=0.85,
        ofi_gain=1.0,
        large_trade_bonus=0.1,
    )
    assert a_low == pytest.approx(0.4)

    a_high = _alpha_shrinkage(
        ofi_abs=2.0,  # |OFI| > 1 saturates at 1.0
        large_trade_flag=1.0,
        t_in_window_s=290.0,
        entry_start_s=60,
        entry_end_s=290,
        alpha_min=0.4,
        alpha_max=0.85,
        ofi_gain=1.0,
        large_trade_bonus=0.1,
    )
    assert a_high == pytest.approx(0.85)


# ---------------- gates ---------------- #


def test_outside_entry_window_skips() -> None:
    d = _strat(model=_StubModel(0.7), ms=_StubMS()).should_enter(
        _ctx(t_in_window=30.0)
    )
    assert d.action is Action.SKIP
    assert d.reason == "outside_entry_window"


def test_spread_too_wide_skips() -> None:
    d = _strat(model=_StubModel(0.7), ms=_StubMS()).should_enter(
        _ctx(spread_bps=400.0)
    )
    assert d.action is Action.SKIP
    assert d.reason == "spread_too_wide"


def test_coinbase_weight_must_be_zero() -> None:
    """Plumbed param that MUST stay 0.0 until coinbase trades land."""
    d = _strat(
        model=_StubModel(0.7),
        ms=_StubMS(),
        overrides={"ofi_coinbase_weight": 1.0},
    ).should_enter(_ctx())
    assert d.action is Action.SKIP
    assert "coinbase" in d.reason


def test_shadow_when_no_model_loaded() -> None:
    """Same shadow-degrade as v3: without a model, p_edge ≡ p_bm
    and the strategy emits SKIP("shadow_mode_no_model") with the
    full feature trail attached."""
    d = _strat(model=None, ms=_StubMS()).should_enter(_ctx())
    assert d.action is Action.SKIP
    assert d.reason == "shadow_mode_no_model"
    # Identity sentinel — never silently invent edge from prior alone.
    assert d.signal_features["p_edge"] == d.signal_features["p_final"]
    assert d.signal_features["p_edge"] == d.signal_features["bb_p_prior"]


def test_full_feature_vector_attached() -> None:
    d = _strat(model=None, ms=_StubMS()).should_enter(_ctx())
    for name in FEATURE_NAMES:
        assert name in d.signal_features, f"missing {name}"


# ---------------- entry path ---------------- #


def test_enter_yes_up_when_model_predicts_high_and_drift_positive() -> None:
    """Spot drifted +17 bps over the window so p_bm > 0.5; model says
    0.85 (much higher than implied 0.65); convex fee at p=0.65 ≈
    0.0287; edge_net ≈ p_final - 0.65 - 0.029 > 0; p_edge_sigma=0.005
    so Sharpe ≫ 2."""
    model = _StubModel(p=0.85)
    d = _strat(model=model, ms=_StubMS()).should_enter(_ctx(implied=0.65))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_UP
    assert d.signal_features["edge_net"] > 0
    assert d.signal_features["sharpe"] >= 2.0


def test_enter_yes_down_when_model_predicts_low() -> None:
    """When p_final is well below p_market, the strategy should pick
    YES_DOWN (the lopsided edge_no side)."""
    model = _StubModel(p=0.10)
    d = _strat(model=model, ms=_StubMS()).should_enter(_ctx(implied=0.65))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_DOWN


def test_shadow_flag_blocks_enter_but_keeps_features() -> None:
    model = _StubModel(p=0.85)
    d = _strat(model=model, ms=_StubMS(), shadow=True).should_enter(
        _ctx(implied=0.65)
    )
    assert d.action is Action.SKIP
    assert d.reason == "shadow_mode"
    assert d.signal_features["p_edge"] == pytest.approx(0.85)
    assert d.signal_features["edge_net"] > 0


def test_sharpe_below_threshold_skips() -> None:
    """Near 50/50 with flat spot drift → p_bm ≈ 0.5, model agrees,
    convex fee at 0.5 = fee_k → edge_net negative → SKIP either via
    the edge floor or the Sharpe gate."""
    model = _StubModel(p=0.50)
    d = _strat(model=model, ms=_StubMS()).should_enter(
        _ctx(spot_drift_bps=0.0, implied=0.50)
    )
    assert d.action is Action.SKIP
    assert d.reason in {"sharpe_below_threshold", "edge_net_below_floor"}


def test_late_window_uses_relaxed_threshold() -> None:
    """t_to_close ≤ 30 s → sharpe_threshold_late (1.5) takes over.
    The decision exposes the effective threshold in signal_features so
    the trail can audit which gate fired."""
    model = _StubModel(p=0.85)
    d = _strat(model=model, ms=_StubMS()).should_enter(
        _ctx(t_in_window=285.0, implied=0.65)
    )
    assert d.signal_features["sharpe_threshold_eff"] == pytest.approx(1.5)


def test_already_entered_window_blocks_second_entry() -> None:
    """Per-window single entry. We poke the internal set directly to
    simulate a prior fill on the same slug."""
    s = _strat(model=_StubModel(p=0.85), ms=_StubMS())
    s._per_window_entered.add("btc-updown-5m-1")
    d = s.should_enter(_ctx(market_slug="btc-updown-5m-1", implied=0.65))
    assert d.action is Action.SKIP
    assert d.reason == "already_entered_this_window"


def test_window_rollover_clears_entered_set() -> None:
    s = _strat(model=_StubModel(p=0.85), ms=_StubMS())
    s._per_window_entered.add("btc-updown-5m-1")
    s.notify_window_rollover("btc-updown-5m-2")
    assert "btc-updown-5m-1" not in s._per_window_entered
