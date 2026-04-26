"""oracle_lag_v1 strategy decision tests."""

from __future__ import annotations

from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.oracle_lag_v1 import OracleLagV1


def _ctx(
    *,
    t_in_window: float,
    spot_price: float,
    open_price: float,
    pm_yes_ask: float = 0.55,
    pm_no_ask: float = 0.46,
    recent_n: int = 80,
) -> TickContext:
    """Build a TickContext with a synthetic recent_ticks history.

    The recent_ticks are 1 Hz spots oscillating gently around
    ``open_price`` to give sigma_ewma a healthy signal without making
    σ huge.
    """
    base_ts = 1_700_000_000.0 + t_in_window
    recent: list[TickContext] = []
    for i in range(recent_n):
        oscill = 1.0 + 0.0001 * ((-1) ** i)  # ±1 bp ping-pong
        recent.append(
            TickContext(
                ts=base_ts - (recent_n - i),
                market_slug="m",
                t_in_window=t_in_window - (recent_n - i),
                window_close_ts=base_ts + (300 - t_in_window),
                spot_price=open_price * oscill,
                chainlink_price=open_price * oscill,
                open_price=open_price,
                pm_yes_bid=pm_yes_ask - 0.01,
                pm_yes_ask=pm_yes_ask,
                pm_no_bid=pm_no_ask - 0.01,
                pm_no_ask=pm_no_ask,
                pm_depth_yes=1000.0,
                pm_depth_no=1000.0,
                pm_imbalance=0.0,
                pm_spread_bps=20.0,
                implied_prob_yes=pm_yes_ask,
                model_prob_yes=0.5,
                edge=0.0,
                z_score=0.0,
                vol_regime="mid",
            )
        )
    return TickContext(
        ts=base_ts,
        market_slug="m",
        t_in_window=t_in_window,
        window_close_ts=base_ts + (300 - t_in_window),
        spot_price=spot_price,
        chainlink_price=spot_price,
        open_price=open_price,
        pm_yes_bid=pm_yes_ask - 0.01,
        pm_yes_ask=pm_yes_ask,
        pm_no_bid=pm_no_ask - 0.01,
        pm_no_ask=pm_no_ask,
        pm_depth_yes=1000.0,
        pm_depth_no=1000.0,
        pm_imbalance=0.0,
        pm_spread_bps=20.0,
        implied_prob_yes=pm_yes_ask,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=0.0,
        vol_regime="mid",
        recent_ticks=recent,
    )


def _cfg(*, shadow: bool = False) -> dict:
    return {
        "params": {
            "entry_window_start_s": 285.0,
            "entry_window_end_s": 297.0,
            "sigma_lookback_s": 90.0,
            "sigma_min_ticks": 60,
            "ewma_lambda": 0.94,
            "fee_a": 0.005,
            "fee_b": 0.025,
            "ev_threshold": 0.005,
            "usdt_basis_phase0": 1.0,
        },
        "paper": {"shadow": shadow},
    }


def test_skip_outside_entry_window() -> None:
    s = OracleLagV1(_cfg())
    s.on_start()
    # T-30s — outside [285, 297]
    ctx = _ctx(t_in_window=270.0, spot_price=67_500.0, open_price=67_500.0)
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "outside_entry_window"


def test_skip_insufficient_sigma_ticks() -> None:
    s = OracleLagV1(_cfg())
    s.on_start()
    ctx = _ctx(t_in_window=290.0, spot_price=67_500.0, open_price=67_500.0, recent_n=10)
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "insufficient_sigma_ticks"


def test_enter_strong_up_signal() -> None:
    """Strong δ + thin σ + short τ → high p_up; if ask is favourable
    (e.g. YES at 0.55 with p_up=0.85), EV well above threshold.
    """
    s = OracleLagV1(_cfg())
    s.on_start()
    # +0.3 % move with τ=10s. σ from the synthetic 1-bp ping-pong should
    # give σ ≈ 1e-4. δ/σ√τ ≈ 0.003/(1e-4·√10) ≈ 9.5 → p_up ≈ 1.0.
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_ask=0.55,  # under-priced relative to p_up≈1
    )
    d = s.should_enter(ctx)
    assert d.action == Action.ENTER, d.reason
    assert d.side == Side.YES_UP
    feats = d.signal_features
    # σ EWMA picks up the +0.3 % step (the discontinuity between the
    # 1-bp ping-pong recent_ticks and the +30 bp current spot), so
    # σ√τ ≈ 0.0024 and δ/σ√τ ≈ 1.25 → Φ(1.25) ≈ 0.89. That's still
    # plenty of edge against a 0.55 ask.
    assert feats["prob_up"] > 0.85
    assert feats["ev_net"] > 0.005


def test_enter_strong_down_signal() -> None:
    s = OracleLagV1(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_300.0,
        open_price=67_500.0,
        pm_no_ask=0.55,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.ENTER, d.reason
    assert d.side == Side.YES_DOWN
    # Mirror of the up case: prob_up ≈ 0.11.
    assert d.signal_features["prob_up"] < 0.15


def test_skip_ev_below_threshold_when_ask_too_high() -> None:
    """High p_up but ask already prices it in → EV after fee < threshold."""
    s = OracleLagV1(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_ask=0.95,  # already prices in the up move
    )
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "ev_below_threshold"
    # Feature trail still attached for offline calibration.
    assert "ev_net" in d.signal_features
    assert d.signal_features["prob_up"] > 0.85


def test_shadow_mode_emits_skip_with_features() -> None:
    s = OracleLagV1(_cfg(shadow=True))
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_ask=0.55,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "shadow_mode"
    # Full feature dict still attached so offline analysis can score it.
    assert d.signal_features["ev_net"] > 0.005


def test_one_entry_per_market() -> None:
    s = OracleLagV1(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_ask=0.55,
    )
    d1 = s.should_enter(ctx)
    assert d1.action == Action.ENTER
    d2 = s.should_enter(ctx)
    assert d2.action == Action.SKIP
    assert d2.reason == "already_entered"
