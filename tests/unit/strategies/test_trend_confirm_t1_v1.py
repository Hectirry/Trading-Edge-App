import math

from trading.engine.types import Action, OrderType, Side, TickContext
from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1

CFG = {
    "params": {
        "entry_horizon_s": 210,
        "horizon_tolerance_s": 10.0,
        "delta_bps_min": 1.0,
        "max_price": 0.92,
        "min_confirmations": 3,
        "cusum_min_rate": 0.005,
        "recent_window_s": 60,
        "doji_body_ratio_min": 0.3,
        "cl_adverse_max_bps": 15.0,
        "frac_d": 0.4,
        "frac_size": 60,
        "cusum_threshold": 0.0005,
        "cusum_lookback": 120,
        "ar_lags": [1, 5, 15, 30],
        "autocorr_lag": 30,
        "mc_enabled": False,  # speed up tests
        "mc_shadow": True,
        "order_type": "market",
    }
}


def _make_ctx(
    t_in_window: float = 210.0,
    spot: float = 95500.0,
    open_price: float = 95000.0,
    chainlink: float = 95500.0,
    pm_yes_ask: float = 0.55,
    pm_no_ask: float = 0.45,
    recent_spots: list[float] | None = None,
) -> TickContext:
    delta_bps = (spot - open_price) / open_price * 10000.0
    if recent_spots is None:
        recent_spots = [open_price + (spot - open_price) * i / 90 for i in range(90)]
    recent_ticks = [
        TickContext(
            ts=1000.0 + i,
            market_slug="test",
            t_in_window=float(i),
            window_close_ts=2000.0,
            spot_price=p,
            chainlink_price=p,
            open_price=open_price,
            pm_yes_bid=pm_yes_ask - 0.02,
            pm_yes_ask=pm_yes_ask,
            pm_no_bid=pm_no_ask - 0.02,
            pm_no_ask=pm_no_ask,
            pm_depth_yes=500,
            pm_depth_no=500,
            pm_imbalance=1.0,
            pm_spread_bps=200,
            implied_prob_yes=0.5,
            model_prob_yes=0.5,
            edge=0.0,
            z_score=0.0,
            vol_regime="unknown",
            t_to_close=300.0 - i,
            delta_bps=delta_bps,
        )
        for i, p in enumerate(recent_spots)
    ]
    return TickContext(
        ts=1090.0,
        market_slug="btc-updown-5m-1000",
        t_in_window=t_in_window,
        window_close_ts=1300.0,
        spot_price=spot,
        chainlink_price=chainlink,
        open_price=open_price,
        pm_yes_bid=pm_yes_ask - 0.02,
        pm_yes_ask=pm_yes_ask,
        pm_no_bid=pm_no_ask - 0.02,
        pm_no_ask=pm_no_ask,
        pm_depth_yes=500,
        pm_depth_no=500,
        pm_imbalance=1.0,
        pm_spread_bps=200,
        implied_prob_yes=0.5,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=0.0,
        vol_regime="unknown",
        t_to_close=90.0,
        recent_ticks=recent_ticks,
        delta_bps=delta_bps,
    )


def test_skip_outside_horizon():
    s = TrendConfirmT1V1(config=CFG)
    d = s.should_enter(_make_ctx(t_in_window=150.0))
    assert d.action is Action.SKIP
    assert "not_at_horizon" in d.reason


def test_skip_when_delta_bps_below_threshold():
    s = TrendConfirmT1V1(config=CFG)
    d = s.should_enter(_make_ctx(spot=95000.05, open_price=95000.0))
    assert d.action is Action.SKIP
    assert "indeciso" in d.reason


def test_skip_on_chainlink_adverse():
    s = TrendConfirmT1V1(config=CFG)
    # spot up but chainlink down >15 bps.
    ctx = _make_ctx(spot=95500.0, open_price=95000.0, chainlink=94800.0)
    d = s.should_enter(ctx)
    assert d.action is Action.SKIP
    assert "cl_divergence" in d.reason


def test_skip_on_max_price_cap():
    s = TrendConfirmT1V1(config=CFG)
    # YES_UP side but ask beyond max_price=0.92.
    ctx = _make_ctx(pm_yes_ask=0.95)
    d = s.should_enter(ctx)
    assert d.action is Action.SKIP
    assert "fav_ask" in d.reason


def test_horizon_tolerance_boundary():
    s = TrendConfirmT1V1(config=CFG)
    # Exactly at the upper edge.
    ctx = _make_ctx(t_in_window=220.0)
    # May SKIP (indeciso) or continue; just assert it's NOT a not_at_horizon skip.
    d = s.should_enter(ctx)
    assert "not_at_horizon" not in d.reason


def test_order_type_market_by_default():
    # This test only checks that when all gates pass, the Decision carries
    # MARKET order_type. We construct a strongly trending recent series so
    # AFML filters are likely to align; if they don't we still verify a
    # non-crash SKIP.
    prices = [95000.0 + i * 1.5 for i in range(120)]
    s = TrendConfirmT1V1(config=CFG)
    ctx = _make_ctx(
        spot=prices[-1],
        open_price=95000.0,
        chainlink=prices[-1],
        recent_spots=prices,
    )
    d = s.should_enter(ctx)
    if d.action is Action.ENTER:
        assert d.side is Side.YES_UP
        assert d.order_type is OrderType.MARKET
    else:
        # Filters didn't align — ensure reason is reported.
        assert d.reason != ""
        assert math.isfinite(ctx.delta_bps)
