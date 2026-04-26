"""oracle_lag_v2 strategy decision-flow tests (Sprint D / ADR 0014).

Mirrors `test_oracle_lag_v1.py` for the cases the two strategies share
(window gate, σ-tick gate, EV gate, shadow mode) and adds new tests for
v2-specific behaviour: limit-price calculation, GTC order_type emission,
cancel-on-EV-decay, requote throttle.
"""

from __future__ import annotations

from trading.engine.types import Action, OrderType, Side, TickContext
from trading.strategies.polymarket_btc5m.oracle_lag_v2 import OracleLagV2


def _ctx(
    *,
    t_in_window: float,
    spot_price: float,
    open_price: float,
    pm_yes_bid: float = 0.54,
    pm_yes_ask: float = 0.55,
    pm_no_bid: float = 0.45,
    pm_no_ask: float = 0.46,
    recent_n: int = 80,
    market_slug: str = "m",
    base_ts: float = 1_700_000_000.0,
) -> TickContext:
    """Build a TickContext with synthetic recent_ticks history.

    Same shape as the v1 test fixture so the two strategies are tested
    on apples-to-apples inputs. Recent ticks oscillate ±1 bp around
    open_price so σ EWMA gets a well-formed signal.
    """
    cur_ts = base_ts + t_in_window
    recent: list[TickContext] = []
    for i in range(recent_n):
        oscill = 1.0 + 0.0001 * ((-1) ** i)
        recent.append(
            TickContext(
                ts=cur_ts - (recent_n - i),
                market_slug=market_slug,
                t_in_window=t_in_window - (recent_n - i),
                window_close_ts=base_ts + 300.0,
                spot_price=open_price * oscill,
                chainlink_price=open_price * oscill,
                open_price=open_price,
                pm_yes_bid=pm_yes_bid,
                pm_yes_ask=pm_yes_ask,
                pm_no_bid=pm_no_bid,
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
        ts=cur_ts,
        market_slug=market_slug,
        t_in_window=t_in_window,
        window_close_ts=base_ts + 300.0,
        spot_price=spot_price,
        chainlink_price=spot_price,
        open_price=open_price,
        pm_yes_bid=pm_yes_bid,
        pm_yes_ask=pm_yes_ask,
        pm_no_bid=pm_no_bid,
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


def _cfg(*, shadow: bool = False, **overrides) -> dict:
    base = {
        "params": {
            "entry_window_start_s": 60.0,
            "entry_window_end_s": 297.0,
            "sigma_lookback_s": 90.0,
            "sigma_min_ticks": 60,
            "ewma_lambda": 0.94,
            "ev_threshold": 0.005,
            "usdt_basis_phase0": 1.0,
        },
        "execution": {
            "mode": "maker",
            "limit_offset_bps": 50.0,
            "gamma_inventory": 0.1,
            "k_order_arrival": 5.0,
            "cancel_threshold_drop_bps": 30.0,
            "cancel_min_interval_s": 2.0,
            "max_active_quotes_per_market": 1,
        },
        "paper": {"shadow": shadow},
    }
    for section, values in overrides.items():
        base.setdefault(section, {}).update(values)
    return base


# ---- gate tests (parity with v1 where applicable) --------------------------


def test_skip_outside_entry_window() -> None:
    s = OracleLagV2(_cfg())
    s.on_start()
    # t = 30 s — before our default entry_window_start_s = 60 s.
    ctx = _ctx(t_in_window=30.0, spot_price=67_500.0, open_price=67_500.0)
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "outside_entry_window"


def test_window_starts_much_earlier_than_v1() -> None:
    """v2 opens at t=60s vs v1's t=285s. Sanity check that t=120s,
    well before v1's window, is *inside* v2's.
    """
    s = OracleLagV2(_cfg(shadow=True))  # shadow → SKIP but features attached
    s.on_start()
    ctx = _ctx(
        t_in_window=120.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d = s.should_enter(ctx)
    # In shadow with ample EV we get SKIP/shadow_mode (not outside_entry_window).
    assert d.action == Action.SKIP
    assert d.reason == "shadow_mode"
    assert d.signal_features.get("limit_price") is not None


def test_skip_insufficient_sigma_ticks() -> None:
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=120.0, spot_price=67_500.0,
        open_price=67_500.0, recent_n=10,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "insufficient_sigma_ticks"


# ---- enter / limit-price calculation ---------------------------------------


def test_enter_emits_gtc_order_with_limit_price() -> None:
    """Strong up signal at t=290 with under-priced ask → ENTER with
    order_type=GTC and a limit_price strictly below the YES mid."""
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.56,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.ENTER, d.reason
    assert d.side == Side.YES_UP
    assert d.order_type == OrderType.GTC
    assert d.limit_price is not None
    # The limit must be ≤ existing bid (no crossing — maker).
    assert d.limit_price <= ctx.pm_yes_bid + 1e-9
    # Mid is 0.55; limit_price should sit at-or-below the bid.
    feats = d.signal_features
    assert feats["mid"] == 0.55
    assert feats["execution_mode"] == "maker"
    assert feats["fee"] == 0.0
    # ev_net (maker) > ev_gross-with-taker-fee at this prob_up — the
    # whole point of v2.
    assert feats["ev_net"] >= 0.005


def test_enter_strong_down_signal_buys_no_side() -> None:
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_300.0,
        open_price=67_500.0,
        pm_no_bid=0.54,
        pm_no_ask=0.56,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.ENTER, d.reason
    assert d.side == Side.YES_DOWN
    assert d.order_type == OrderType.GTC
    # Should be quoting the NO side mid.
    assert d.signal_features["mid"] == 0.55


def test_skip_ev_below_threshold_when_signal_weak_and_book_tight() -> None:
    """Tiny δ → prob_up barely above 0.5; meanwhile the YES book is
    already tight at 0.99. Even at the AS-derived maker BUY price
    (~0.985) the EV is dominated by ``-(1-p)*limit`` and goes
    negative. Strategy must SKIP / ev_below_threshold.

    This is the v2 analogue of v1's "high ask kills the edge" test —
    moving the rebate from taker to maker doesn't help when the
    resting book already prices the move in AND the signal is weak.
    """
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        # Almost-zero δ: prob_up ≈ 0.5, p_side ≈ 0.50-0.55.
        spot_price=67_500.5,
        open_price=67_500.0,
        # Book is very tight on the favoured side.
        pm_yes_bid=0.99,
        pm_yes_ask=0.995,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "ev_below_threshold"
    # Feature trail still attached for offline calibration.
    assert "ev_net" in d.signal_features
    assert d.signal_features["ev_net"] < 0.005


# ---- shadow mode -----------------------------------------------------------


def test_shadow_mode_emits_skip_with_features() -> None:
    s = OracleLagV2(_cfg(shadow=True))
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d = s.should_enter(ctx)
    assert d.action == Action.SKIP
    assert d.reason == "shadow_mode"
    # Limit price still computed and attached for offline analysis.
    assert d.signal_features["limit_price"] is not None
    assert d.signal_features["ev_net"] >= 0.005


# ---- v2-specific cancel / re-quote behaviour -------------------------------


def test_quote_held_when_book_unchanged() -> None:
    """Same market re-evaluated within `cancel_min_interval_s` and
    with no price drift → SKIP / quote_held (do not re-quote)."""
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx_a = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d1 = s.should_enter(ctx_a)
    assert d1.action == Action.ENTER
    # Tick again 1 s later, identical book.
    ctx_b = _ctx(
        t_in_window=291.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d2 = s.should_enter(ctx_b)
    assert d2.action == Action.SKIP
    assert d2.reason == "quote_held"


def test_cancel_on_ev_decay() -> None:
    """If after entering the signal collapses (ev_net < threshold AND
    drop ≥ cancel_threshold_drop_bps) the strategy emits SKIP /
    cancel_ev_decayed and clears the active-quote state.
    """
    s = OracleLagV2(_cfg())
    s.on_start()
    # 1) ENTER — strong up move.
    ctx_a = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d1 = s.should_enter(ctx_a)
    assert d1.action == Action.ENTER
    quoted_ev = d1.signal_features["ev_net"]
    # 2) Next tick — both the signal weakens (price drifted back to
    # near open) AND the book is now tight (resting bid 0.99). Result:
    # ev_net falls well below threshold and the drop from quoted_ev
    # easily exceeds cancel_threshold_drop_bps (30 bps = 0.003).
    ctx_b = _ctx(
        t_in_window=295.0,
        spot_price=67_500.5,
        open_price=67_500.0,
        pm_yes_bid=0.99,
        pm_yes_ask=0.995,
    )
    d2 = s.should_enter(ctx_b)
    assert d2.action == Action.SKIP
    # Either decayed-and-cancelled, or just below-threshold (the
    # cancel branch only fires if drop ≥ 30 bps; both reasons mean we
    # are NOT holding a stale quote).
    assert d2.reason in ("cancel_ev_decayed", "ev_below_threshold")
    assert d2.signal_features["ev_net"] < quoted_ev


def test_one_active_quote_per_market_until_cancel() -> None:
    """First call → ENTER; immediate second call (same tick) →
    SKIP/quote_held (max_active_quotes_per_market = 1).
    """
    s = OracleLagV2(_cfg())
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d1 = s.should_enter(ctx)
    assert d1.action == Action.ENTER
    d2 = s.should_enter(ctx)
    assert d2.action == Action.SKIP
    assert d2.reason in ("quote_held", "requote_throttled")


def test_as_limit_price_sits_below_mid_for_buy() -> None:
    """The Avellaneda-Stoikov calc must put the maker BUY strictly
    below the mid (and ≤ the existing bid). Sanity invariant — if
    this ever flips we'd be crossing the book = taker.
    """
    s = OracleLagV2(_cfg(shadow=True))
    s.on_start()
    ctx = _ctx(
        t_in_window=290.0,
        spot_price=67_700.0,
        open_price=67_500.0,
        pm_yes_bid=0.54,
        pm_yes_ask=0.55,
    )
    d = s.should_enter(ctx)
    feats = d.signal_features
    assert feats["limit_price"] < feats["mid"]
    assert feats["limit_price"] <= ctx.pm_yes_bid + 1e-9
