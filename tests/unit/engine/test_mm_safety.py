"""Unit tests for MMSafetyGuard (no-bypass safety gates)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading.engine.mm_safety import MMSafetyGuard, MMSafetyParams


def _now() -> datetime:
    return datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


# ─── inventory cap ──────────────────────────────────────────────────


def test_inventory_cap_allows_below_threshold():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(inventory_cap_usdc=50.0))
    blocked, _ = g.block_post_quote("slug-x", side_sign=+1, qty_shares=100, p_market=0.18)
    # 100 shares × 0.18 = $18 < $50 cap → allowed.
    assert blocked is False


def test_inventory_cap_blocks_at_threshold():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(inventory_cap_usdc=10.0))
    blocked, reason = g.block_post_quote("slug-x", side_sign=+1, qty_shares=100, p_market=0.18)
    # 100 × 0.18 = $18 > $10 → block.
    assert blocked is True
    assert "inventory_cap_usdc breach" in reason


def test_inventory_cap_considers_existing_position():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(inventory_cap_usdc=50.0))
    # Pre-fill 200 shares long YES at 0.18 ⇒ q × p = $36.
    g.record_inventory_after_fill("slug-x", side_sign=+1, qty_shares=200)
    # PostQuote on bid (BUY YES) for 100 more would bring q × p ≈ $54 > $50 → block.
    blocked, _ = g.block_post_quote("slug-x", side_sign=+1, qty_shares=100, p_market=0.18)
    assert blocked is True
    # Sell side (would reduce |q|) is fine.
    blocked2, _ = g.block_post_quote("slug-x", side_sign=-1, qty_shares=100, p_market=0.18)
    assert blocked2 is False


def test_record_inventory_signed():
    g = MMSafetyGuard("mm_rebate_v1")
    g.record_inventory_after_fill("slug-x", side_sign=+1, qty_shares=50)
    g.record_inventory_after_fill("slug-x", side_sign=-1, qty_shares=20)
    assert g.inventory_shares("slug-x") == 30.0


# ─── terminal-window ────────────────────────────────────────────────


def test_terminal_window_block():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(tau_terminal_s=60.0))
    blocked, _ = g.block_post_quote_terminal(t_in_window_s=850, window_seconds=900)
    assert blocked is True


def test_terminal_window_allows_early():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(tau_terminal_s=60.0))
    blocked, _ = g.block_post_quote_terminal(t_in_window_s=300, window_seconds=900)
    assert blocked is False


# ─── cancel/fill ratio ──────────────────────────────────────────────


def test_cancel_fill_ratio_no_breach_below_threshold():
    g = MMSafetyGuard(
        "mm_rebate_v1", MMSafetyParams(cancel_fill_ratio_max=10.0, auto_kill_on_breach=True)
    )
    now = _now()
    g.record_fill("slug-x", now=now)
    for i in range(5):
        g.record_cancel("slug-x", now=now + timedelta(seconds=i))
    # 5 cancels / 1 fill = 5.0 < 10 → no breach.
    assert g.alerts == []
    assert g.is_market_killed("slug-x") is False


def test_cancel_fill_ratio_alert_on_first_breach():
    g = MMSafetyGuard(
        "mm_rebate_v1", MMSafetyParams(cancel_fill_ratio_max=10.0, auto_kill_on_breach=False)
    )
    now = _now()
    g.record_fill("slug-x", now=now)
    for i in range(15):
        g.record_cancel("slug-x", now=now + timedelta(seconds=i))
    # 15/1 = 15 > 10 → breach. With auto_kill=False, alert only.
    assert len(g.alerts) >= 1
    assert g.alerts[-1]["kind"] == "cancel_fill_breach"
    assert g.is_market_killed("slug-x") is False  # paper: no auto-kill


def test_cancel_fill_ratio_kill_on_2nd_breach_when_auto_kill_on():
    g = MMSafetyGuard(
        "mm_rebate_v1",
        MMSafetyParams(
            cancel_fill_ratio_max=10.0,
            cancel_fill_kill_threshold=2,
            cancel_fill_kill_window_min=60,
            cancel_fill_resume_min=15,
            auto_kill_on_breach=True,
        ),
    )
    now = _now()
    # First breach at t0
    g.record_fill("slug-x", now=now)
    for i in range(15):
        g.record_cancel("slug-x", now=now + timedelta(seconds=i))
    assert not g.is_market_killed("slug-x", now=now + timedelta(seconds=20))
    # Second breach 5 min later
    later = now + timedelta(minutes=5)
    g.record_fill("slug-x", now=later)
    for i in range(15):
        g.record_cancel("slug-x", now=later + timedelta(seconds=i))
    # Now killed for 15 minutes
    assert g.is_market_killed("slug-x", now=later + timedelta(minutes=1))
    # And resumes after 15 min
    assert not g.is_market_killed("slug-x", now=later + timedelta(minutes=20))


def test_old_breach_events_age_out_of_kill_window():
    """A breach >60min ago should not count toward the 2nd-breach kill rule."""
    g = MMSafetyGuard(
        "mm_rebate_v1",
        MMSafetyParams(
            cancel_fill_ratio_max=10.0,
            cancel_fill_kill_threshold=2,
            cancel_fill_kill_window_min=60,
            auto_kill_on_breach=True,
        ),
    )
    now = _now()
    g.record_fill("slug-x", now=now)
    for i in range(15):
        g.record_cancel("slug-x", now=now + timedelta(seconds=i))
    # Wait 90 min — the 1st breach should age out before the 2nd.
    much_later = now + timedelta(minutes=90)
    g.record_fill("slug-x", now=much_later)
    for i in range(15):
        g.record_cancel("slug-x", now=much_later + timedelta(seconds=i))
    # Breach is recorded but kill threshold needs 2 within the window.
    # Old breach has aged out → only 1 breach in window → not killed.
    assert not g.is_market_killed("slug-x", now=much_later + timedelta(seconds=30))


# ─── taker fee canary ───────────────────────────────────────────────


def test_canary_below_threshold():
    g = MMSafetyGuard(
        "mm_rebate_v1", MMSafetyParams(taker_fee_canary_pct=0.05)
    )
    g.record_pnl_gross(100.0)
    g.record_taker_fee_paid(2.0)
    assert g.canary_ratio() == 0.02
    assert g.canary_breached() is False


def test_canary_at_breach():
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(taker_fee_canary_pct=0.05))
    g.record_pnl_gross(100.0)
    g.record_taker_fee_paid(10.0)
    assert g.canary_ratio() == 0.10
    assert g.canary_breached() is True


def test_canary_zero_pnl_returns_zero():
    """Defensive: if no gross PnL, ratio is 0 to avoid div0 / inf."""
    g = MMSafetyGuard("mm_rebate_v1")
    g.record_taker_fee_paid(5.0)
    assert g.canary_ratio() == 0.0
    assert g.canary_breached() is False


def test_canary_window_ages_out():
    """7-day rolling window: events older than 7 days are evicted."""
    g = MMSafetyGuard("mm_rebate_v1", MMSafetyParams(taker_fee_canary_window_days=7))
    old = _now() - timedelta(days=10)
    g.record_pnl_gross(1000.0, now=old)
    g.record_taker_fee_paid(500.0, now=old)
    # New ratio computation should evict and return 0.
    g.record_pnl_gross(0.01)  # triggers gc with current now
    g.record_taker_fee_paid(0.0)
    # Old data evicted; only fresh entries remain.
    assert g.canary_ratio() == 0.0


# ─── auto_kill_on_breach=false (paper mode) ─────────────────────────


def test_paper_mode_alerts_but_does_not_kill():
    """Aggressive paper soak: surfacing pathology, not blocking."""
    g = MMSafetyGuard(
        "mm_rebate_v1", MMSafetyParams(cancel_fill_ratio_max=5.0, auto_kill_on_breach=False)
    )
    now = _now()
    for i in range(20):
        g.record_cancel("slug-x", now=now + timedelta(seconds=i))
    # Many breaches but no fills ⇒ ratio infinite-ish, alerts fire,
    # killed_until stays None.
    assert len(g.alerts) >= 1
    assert g.is_market_killed("slug-x") is False
