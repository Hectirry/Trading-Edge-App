from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3


def _ctx(
    ts: float = 1000.0,
    t_in_window: float = 130.0,
    imb: float = 1.10,
    spread_bps: float = 150,
    depth_yes: float = 500.0,
    depth_no: float = 500.0,
) -> TickContext:
    return TickContext(
        ts=ts,
        market_slug="btc-updown-5m-1300",
        t_in_window=t_in_window,
        window_close_ts=ts + 170,
        spot_price=95000.0,
        chainlink_price=95000.0,
        open_price=95000.0,
        pm_yes_bid=0.50,
        pm_yes_ask=0.51,
        pm_no_bid=0.49,
        pm_no_ask=0.50,
        pm_depth_yes=depth_yes,
        pm_depth_no=depth_no,
        pm_imbalance=imb,
        pm_spread_bps=spread_bps,
        implied_prob_yes=0.5,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=1.0,
        vol_regime="low",
        t_to_close=170,
    )


CFG = {
    "params": {
        "imbalance_threshold": 1.05,
        "min_depth_total_usd": 300.0,
        "max_spread_bps": 200.0,
        "require_depth_trend_min_pct": -5.0,
        "allowed_sides": ["YES_UP"],
        "blocked_hours_utc": [],
        "max_consecutive_losses": 3,
        "streak_pause_minutes": 30,
    }
}


def test_enter_when_all_filters_pass():
    s = ImbalanceV3(config=CFG)
    d = s.should_enter(_ctx(imb=1.10))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_UP


def test_skip_when_imbalance_neutral():
    s = ImbalanceV3(config=CFG)
    d = s.should_enter(_ctx(imb=1.02))
    assert d.action is Action.SKIP
    assert "neutral" in d.reason


def test_skip_when_spread_too_wide():
    s = ImbalanceV3(config=CFG)
    d = s.should_enter(_ctx(imb=1.10, spread_bps=250))
    assert d.action is Action.SKIP
    assert "spread" in d.reason


def test_skip_when_depth_too_low():
    s = ImbalanceV3(config=CFG)
    d = s.should_enter(_ctx(imb=1.10, depth_yes=50, depth_no=50))
    assert d.action is Action.SKIP
    assert "depth" in d.reason


def test_yes_down_disabled_by_default():
    s = ImbalanceV3(config=CFG)
    # imb=0.5 → imb_inv=2.0 ≥ threshold → would trigger YES_DOWN if allowed.
    d = s.should_enter(_ctx(imb=0.5))
    assert d.action is Action.SKIP
    assert "YES_DOWN" in d.reason


def test_streak_pause_after_losses():
    s = ImbalanceV3(config=CFG)
    # Three losses should arm the pause; confirm subsequent tick is blocked.
    s.on_trade_resolved("loss", -3.0, ts=0.0)
    s.on_trade_resolved("loss", -3.0, ts=100.0)
    s.on_trade_resolved("loss", -3.0, ts=200.0)
    d = s.should_enter(_ctx(ts=201.0, imb=1.10))
    assert d.action is Action.SKIP
    assert "streak_pause" in d.reason
    # 30 minutes later, pause expires.
    d2 = s.should_enter(_ctx(ts=200.0 + 31 * 60, imb=1.10))
    assert d2.action is Action.ENTER


def test_win_breaks_streak():
    s = ImbalanceV3(config=CFG)
    s.on_trade_resolved("loss", -3.0, ts=0.0)
    s.on_trade_resolved("loss", -3.0, ts=100.0)
    assert s.consecutive_losses == 2
    s.on_trade_resolved("win", 2.88, ts=200.0)
    assert s.consecutive_losses == 0
