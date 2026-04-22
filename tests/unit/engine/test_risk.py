from types import SimpleNamespace

from trading.engine.risk import RiskManager

BASE_CFG = {
    "risk": {
        "cooldown_seconds": 30,
        "max_position_size_usd": 5.0,
        "daily_loss_limit_usd": 50.0,
        "daily_trade_limit": 999999,
        "min_edge_bps": 10,
        "min_z_score": 0.5,
        "min_pm_depth_usd": 15.0,
        "skip_if_spread_bps": 500,
        "loss_pause_threshold_usd": 5.0,
        "loss_pause_window_minutes": 30,
        "loss_pause_duration_minutes": 30,
    }
}


def _ctx(ts: float, z: float = 1.0, edge: float = 0.01, spread: float = 100, depth: float = 200):
    return SimpleNamespace(
        ts=ts,
        z_score=z,
        edge=edge,
        pm_spread_bps=spread,
        pm_depth_yes=depth,
        pm_depth_no=depth,
    )


def test_risk_accepts_clean_tick():
    r = RiskManager(BASE_CFG)
    assert r.can_enter(_ctx(1000.0)) == (True, "ok")


def test_risk_rejects_low_z():
    r = RiskManager(BASE_CFG)
    ok, reason = r.can_enter(_ctx(1000.0, z=0.2))
    assert ok is False
    assert "z_score" in reason


def test_risk_rejects_wide_spread():
    r = RiskManager(BASE_CFG)
    ok, reason = r.can_enter(_ctx(1000.0, spread=600))
    assert ok is False
    assert "spread" in reason


def test_risk_rejects_low_depth():
    r = RiskManager(BASE_CFG)
    ok, reason = r.can_enter(_ctx(1000.0, depth=5))
    assert ok is False
    assert "depth" in reason


def test_risk_cooldown_after_trade():
    r = RiskManager(BASE_CFG)
    r.on_trade_closed(pnl=-1.0, now=1000.0)
    ok, reason = r.can_enter(_ctx(1010.0))
    assert ok is False
    assert "cooldown" in reason
    # After 30s cooldown expires.
    assert r.can_enter(_ctx(1031.0))[0] is True


def test_risk_circuit_breaker_on_daily_loss():
    # Disable rolling-loss pause so daily-loss circuit breaker is the only gate.
    cfg = {"risk": {**BASE_CFG["risk"], "loss_pause_threshold_usd": 0}}
    r = RiskManager(cfg)
    r.on_trade_closed(pnl=-50.0, now=1000.0)
    # First call trips the breaker; reason is the raw trip reason.
    ok1, reason1 = r.can_enter(_ctx(2000.0))
    assert ok1 is False
    assert "daily_loss_limit" in reason1
    # Subsequent calls return the circuit_breaker prefix.
    ok2, reason2 = r.can_enter(_ctx(3000.0))
    assert ok2 is False
    assert "circuit_breaker" in reason2


def test_risk_rolling_loss_pauses():
    r = RiskManager(BASE_CFG)
    r.on_trade_closed(pnl=-3.0, now=1000.0)
    r.on_trade_closed(pnl=-3.0, now=1100.0)
    # Rolling loss = -$6 > $5 threshold → pause active at 1200.
    ok, reason = r.can_enter(_ctx(1200.0))
    assert ok is False
    assert "cool-off" in reason or "cooldown" in reason
