import math

from trading.engine.indicators import (
    EMA,
    RSI,
    EWMAVol,
    IndicatorStack,
    RealizedVol,
    _norm_cdf,
    black_scholes_binary_prob,
    rolling_zscore,
)
from trading.engine.types import TickContext


def _ctx(
    ts: float, spot: float, open_price: float, impl_prob: float, t_to_close: float
) -> TickContext:
    return TickContext(
        ts=ts,
        market_slug="btc-updown-5m-0",
        t_in_window=0.0,
        window_close_ts=ts + t_to_close,
        spot_price=spot,
        chainlink_price=None,
        open_price=open_price,
        pm_yes_bid=0.5,
        pm_yes_ask=0.51,
        pm_no_bid=0.49,
        pm_no_ask=0.5,
        pm_depth_yes=500,
        pm_depth_no=500,
        pm_imbalance=1.0,
        pm_spread_bps=200,
        implied_prob_yes=impl_prob,
        model_prob_yes=0.0,
        edge=0.0,
        z_score=0.0,
        vol_regime="unknown",
        t_to_close=t_to_close,
    )


def test_ema_initialization_and_converge():
    e = EMA(period=10)
    assert e.update(100.0) == 100.0
    assert e.initialized is True
    v1 = e.update(110.0)
    assert 100.0 < v1 < 110.0


def test_rsi_returns_50_when_cold():
    r = RSI(period=14)
    assert r.update(100.0) == 50.0
    # 5 bars same direction, still below period — still 50
    for _ in range(5):
        assert r.update(101.0) == 50.0


def test_rsi_all_up_approaches_100():
    r = RSI(period=14)
    r.update(100.0)
    for i in range(1, 20):
        r.update(100.0 + i)
    final = r.update(120.0)
    assert final > 95


def test_realized_vol_zero_when_insufficient():
    v = RealizedVol(window_seconds=10)
    assert v.update(100.0) == 0.0
    assert v.update(100.1) == 0.0  # still n<2 after 1 return


def test_ewma_vol_zero_on_first():
    v = EWMAVol(lam=0.94)
    assert v.update(100.0) == 0.0
    v2 = v.update(100.1)
    assert v2 > 0


def test_black_scholes_flat_when_atm():
    # S = K → d2 slightly negative (drag term), prob ≈ 0.5
    p = black_scholes_binary_prob(S=100.0, K=100.0, sigma=0.5, T=1e-3)
    assert 0.48 < p < 0.52


def test_black_scholes_far_otm_zero():
    p = black_scholes_binary_prob(S=50.0, K=100.0, sigma=0.5, T=1e-5)
    assert p < 0.001


def test_black_scholes_far_itm_one():
    p = black_scholes_binary_prob(S=150.0, K=100.0, sigma=0.5, T=1e-5)
    assert p > 0.999


def test_black_scholes_edge_cases_return_half():
    assert black_scholes_binary_prob(0.0, 1.0, 0.5, 1.0) == 0.5
    assert black_scholes_binary_prob(1.0, 1.0, 0.0, 1.0) == 0.5
    assert black_scholes_binary_prob(1.0, 1.0, 0.5, 0.0) == 0.5


def test_rolling_zscore_basic():
    # Constant series → std=0 → denom guard returns 0 per the code path
    assert rolling_zscore([1, 1, 1, 1], 3) == 0.0
    # Rising series: last value should be positive z
    z = rolling_zscore([1, 2, 3, 4, 5], 5)
    assert z > 0


def test_norm_cdf_monotonic():
    assert _norm_cdf(-2.0) < _norm_cdf(0.0) < _norm_cdf(2.0)
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-9


def test_indicator_stack_updates_ctx_fields():
    stack = IndicatorStack()
    ctx = _ctx(ts=1000.0, spot=95000.0, open_price=95000.0, impl_prob=0.51, t_to_close=200.0)
    stack.update(ctx)
    # All derived fields are set.
    assert ctx.ema_fast == 95000.0
    assert ctx.ema_slow == 95000.0
    assert ctx.vol_ewma == 0.0  # first tick, no return yet
    # Model prob around 0.5 for ATM + tiny T + sigma fallback.
    assert 0.48 < ctx.model_prob_yes < 0.52


def test_indicator_stack_edge_tracks_recent():
    stack = IndicatorStack()
    # Push two ticks with rising spot so vol/edge have meaningful changes.
    c1 = _ctx(ts=1000.0, spot=95000.0, open_price=95000.0, impl_prob=0.50, t_to_close=200.0)
    stack.update(c1)
    c2 = _ctx(ts=1001.0, spot=95200.0, open_price=95000.0, impl_prob=0.50, t_to_close=199.0)
    stack.update(c2)
    # Edge should move in response to model_prob shift.
    assert math.isfinite(c2.edge)
    assert math.isfinite(c2.z_score)
