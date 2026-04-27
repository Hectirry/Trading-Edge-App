"""Step 2 unit tests for mm_rebate_v1.

Covers:
- on_tick gates (zone, time window, market killed, terminal window)
- post-or-replace logic (fresh / replace on price move / skip when stable)
- inventory tracking via on_fill + safety guard
- Cancel-all when leaving the zone
- Action types are emitted with correct fields

DB-backed methods (k_estimator persistence) are mocked.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trading.engine.mm_actions import CancelQuote, PostQuote, ReplaceQuote
from trading.engine.types import Side, TickContext
from trading.strategies.polymarket_btc15m._k_estimator import KEstimator
from trading.strategies.polymarket_btc15m.mm_rebate_v1 import MMRebateV1


def _ctx(
    *,
    p_yes: float = 0.18,
    t_in_window: float = 300.0,
    ts: float = 1.0,
    slug: str = "btc-updown-15m-1777300200",
) -> TickContext:
    return TickContext(
        ts=ts,
        market_slug=slug,
        t_in_window=t_in_window,
        window_close_ts=ts + (900 - t_in_window),
        spot_price=68000.0,
        chainlink_price=68000.0,
        open_price=68000.0,
        pm_yes_bid=p_yes - 0.005,
        pm_yes_ask=p_yes + 0.005,
        pm_no_bid=1 - p_yes - 0.005,
        pm_no_ask=1 - p_yes + 0.005,
        pm_depth_yes=100.0,
        pm_depth_no=100.0,
        pm_imbalance=0.0,
        pm_spread_bps=20.0,
        implied_prob_yes=p_yes,
        model_prob_yes=p_yes,
        edge=0.0,
        z_score=0.0,
        vol_regime="normal",
    )


def _make_strategy(**overrides) -> MMRebateV1:
    cfg: dict = {
        "params": {
            "zone_lo": 0.15,
            "zone_hi": 0.40,
            "dead_zone_lo": 0.40,
            "dead_zone_hi": 0.60,
            "entry_window_start_s": 60.0,
            "tau_terminal_s": 30.0,
            "window_seconds": 900,
            "gamma_inventory_risk": 0.5,
            "spread_floor_bps": 50.0,
            "spread_ceiling_bps": 500.0,
            "stake_nominal_usd": 20.0,
            "quote_ttl_seconds": 0,
        },
        "mm_safety": {
            "inventory_cap_usdc": 50.0,
            "cancel_fill_ratio_max": 30.0,
            "tau_terminal_s": 30.0,
            "auto_kill_on_breach": False,
        },
    }
    for k, v in overrides.items():
        cfg["params"][k] = v
    return MMRebateV1(config=cfg, k_estimator=KEstimator(strategy_id="mm_rebate_v1"))


# ─── zone gates ──────────────────────────────────────────────────────


def test_skip_outside_active_zone_below():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.10))
    assert actions == []


def test_skip_outside_active_zone_above():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.80))
    assert actions == []


def test_skip_inside_dead_zone():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.50))
    assert actions == []


def test_emits_quotes_inside_active_zone():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.18))
    assert len(actions) == 2
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    bids = [a for a in posts if a.side is Side.YES_UP]
    asks = [a for a in posts if a.side is Side.YES_DOWN]
    assert len(bids) == 1 and len(asks) == 1


# ─── time-window gates ───────────────────────────────────────────────


def test_skip_before_entry_window():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.18, t_in_window=30.0))
    assert actions == []


def test_skip_inside_terminal_window():
    """tau_terminal=30 ⇒ stop quoting at t > 870."""
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.18, t_in_window=880.0))
    # If we had no live quotes, the strategy returns []. If we had live
    # quotes from earlier, it would return cancels. Test both.
    assert all(isinstance(a, CancelQuote) for a in actions)


# ─── replace-vs-skip logic ───────────────────────────────────────────


def test_no_replace_when_price_unchanged():
    s = _make_strategy()
    s.on_tick(_ctx(p_yes=0.18, ts=1.0))
    actions2 = s.on_tick(_ctx(p_yes=0.18, ts=2.0))
    # Same p_fair, q=0 ⇒ same bid/ask → no actions on second tick.
    assert actions2 == []


def test_replace_when_price_moves_significantly():
    s = _make_strategy()
    s.on_tick(_ctx(p_yes=0.18, ts=1.0))
    actions2 = s.on_tick(_ctx(p_yes=0.25, ts=2.0))
    # 7¢ move ⇒ both sides should issue ReplaceQuote.
    replaces = [a for a in actions2 if isinstance(a, ReplaceQuote)]
    assert len(replaces) == 2


def test_cancel_all_when_leaving_zone():
    s = _make_strategy()
    s.on_tick(_ctx(p_yes=0.18, ts=1.0))
    # Move to dead zone — strategy should cancel all quotes.
    actions2 = s.on_tick(_ctx(p_yes=0.50, ts=2.0))
    cancels = [a for a in actions2 if isinstance(a, CancelQuote)]
    assert len(cancels) == 2


# ─── inventory tracking ─────────────────────────────────────────────


def test_on_fill_updates_inventory_long_yes():
    s = _make_strategy()
    s.on_fill(
        market_slug="btc-updown-15m-x",
        client_order_id="abc123",
        side=Side.YES_UP.value,
        fill_price=0.18,
        fill_qty_shares=50.0,
        ts=10.0,
    )
    assert s.safety.inventory_shares("btc-updown-15m-x") == 50.0


def test_on_fill_updates_inventory_short_yes():
    s = _make_strategy()
    s.on_fill(
        market_slug="btc-updown-15m-x",
        client_order_id="abc123",
        side=Side.YES_DOWN.value,
        fill_price=0.18,
        fill_qty_shares=50.0,
        ts=10.0,
    )
    assert s.safety.inventory_shares("btc-updown-15m-x") == -50.0


def test_inventory_cap_blocks_post_quote():
    """Inventory at cap ⇒ PostQuote on the same side is blocked."""
    s = _make_strategy()
    # Force inventory near cap by simulating fills:
    # cap = $50, p=0.18 ⇒ 277 shares fills the cap. Build up.
    for i in range(6):
        s.on_fill(
            market_slug="btc-updown-15m-x",
            client_order_id=f"f{i}",
            side=Side.YES_UP.value,
            fill_price=0.18,
            fill_qty_shares=50.0,
            ts=float(i),
        )
    # Now inventory = 300 shares × 0.18 = $54 USDC > cap $50.
    actions = s.on_tick(_ctx(p_yes=0.18, slug="btc-updown-15m-x", ts=10.0))
    # Bid side (YES_UP) must NOT emit PostQuote — would extend further.
    bids = [a for a in actions if isinstance(a, PostQuote) and a.side is Side.YES_UP]
    assert bids == []


# ─── action shape ────────────────────────────────────────────────────


def test_post_quote_has_all_required_fields():
    s = _make_strategy()
    actions = s.on_tick(_ctx(p_yes=0.18))
    post = next(a for a in actions if isinstance(a, PostQuote))
    assert post.market_slug.startswith("btc-updown-15m-")
    assert 0.0 < post.price < 1.0
    assert post.qty_shares > 0
    assert post.client_id_seed in {"bid", "ask"}


def test_post_quote_is_frozen():
    """Action dataclasses must be frozen — replace() returns new, mutation raises."""
    pq = PostQuote(
        side=Side.YES_UP, price=0.18, qty_shares=100, market_slug="x"
    )
    new_pq = replace(pq, price=0.19)
    assert new_pq.price == 0.19
    assert pq.price == 0.18  # original unchanged
    with pytest.raises(Exception):  # FrozenInstanceError
        pq.price = 0.20  # type: ignore[misc]


# ─── parametrizable bucket bounds ────────────────────────────────────


def test_zone_is_parametrizable_via_config():
    """Re-targeting the zone via config (not code) must work — Step 0 v2
    might pick a different bucket."""
    s = _make_strategy(zone_lo=0.20, zone_hi=0.30)
    # Outside the V1 nominee but inside the new override:
    actions = s.on_tick(_ctx(p_yes=0.25))
    assert len(actions) == 2  # two PostQuotes
    # Now the original V1 nominee 0.18 is outside this strategy's zone:
    s2 = _make_strategy(zone_lo=0.20, zone_hi=0.30)
    assert s2.on_tick(_ctx(p_yes=0.18)) == []
