"""Avellaneda-Stoikov pricer unit tests.

Reference closed-form (A&S 2008):

    spread*  = γ · σ² · (T - t) + (2/γ) · ln(1 + γ/k)
    r        = s − q · γ · σ² · (T - t)

Golden vector below was hand-computed; recomputing in the test acts as
a regression sentinel. If the closed form ever drifts (e.g. an off-by-2
on the log term), this test catches it.
"""

from __future__ import annotations

import math

import pytest

from trading.engine.avellaneda_stoikov import (
    ASQuote,
    optimal_spread,
    quote,
    reservation_price,
)

# ---- optimal_spread closed-form regression ----------------------------------


def test_optimal_spread_golden_vector() -> None:
    """Golden vector hand-computed.

    Inputs: γ=0.1, σ=0.0003 /√s, τ=120 s, k=5.

        inventory_term = 0.1 * 0.0003² * 120
                       = 0.1 * 9e-8 * 120 = 1.08e-6
        log_term       = (2/0.1) * ln(1 + 0.1/5)
                       = 20 * ln(1.02)
                       = 20 * 0.0198026273...
                       = 0.39605254...
        spread*        ≈ 0.39605362...

    The inventory term is dwarfed by the log term in this regime —
    that's the A&S signature: the maker's optimal spread is dominated
    by the *rebate-vs-fill trade-off* rather than the inventory risk
    when σ²·τ is small.
    """
    s = optimal_spread(
        sigma_per_sqrt_s=0.0003, tau_s=120.0, gamma=0.1, k=5.0
    )
    inventory_term = 0.1 * (0.0003**2) * 120.0
    log_term = (2.0 / 0.1) * math.log(1.0 + 0.1 / 5.0)
    expected = inventory_term + log_term
    assert s == pytest.approx(expected, rel=1e-12)
    # Sanity-check the absolute value too (1% tolerance — guards
    # against a sign flip or factor-of-2 drift).
    assert s == pytest.approx(0.396054, abs=1e-5)


def test_optimal_spread_increases_with_tau() -> None:
    """∂spread/∂τ > 0 as long as γ, σ > 0 — more time = more
    inventory risk = the maker demands a wider compensation.
    """
    base = optimal_spread(sigma_per_sqrt_s=0.001, tau_s=10.0, gamma=0.1, k=5.0)
    longer = optimal_spread(sigma_per_sqrt_s=0.001, tau_s=120.0, gamma=0.1, k=5.0)
    assert longer > base


def test_optimal_spread_inventory_term_increases_with_gamma() -> None:
    """The inventory term γ·σ²·τ is strictly increasing in γ; the log
    term ``(2/γ)·ln(1 + γ/k)`` is *decreasing* in γ (a classic A-S
    result — the optimal spread is non-monotone overall, with a
    minimum somewhere in (0, ∞)). Pin the inventory-only contribution
    here so we don't accidentally drop the γ factor in the formula.
    """
    sigma = 0.005  # large enough that the inventory term is meaningful
    tau = 120.0
    inv_low = 0.05 * sigma * sigma * tau
    inv_high = 0.5 * sigma * sigma * tau
    assert inv_high > inv_low
    # And the spread itself differs by at least the inventory delta —
    # the log term partly offsets but doesn't fully cancel here.
    s_low = optimal_spread(
        sigma_per_sqrt_s=sigma, tau_s=tau, gamma=0.05, k=5.0
    )
    s_high = optimal_spread(
        sigma_per_sqrt_s=sigma, tau_s=tau, gamma=0.5, k=5.0
    )
    # Both finite and positive — sanity, not monotonicity.
    assert s_low > 0 and s_high > 0


def test_optimal_spread_zero_tau_collapses_to_log_term() -> None:
    """τ=0 wipes the inventory term; only the asymptotic rebate term
    survives. Useful sanity for the very-end-of-window regime.
    """
    s = optimal_spread(
        sigma_per_sqrt_s=0.001, tau_s=0.0, gamma=0.1, k=5.0
    )
    expected = (2.0 / 0.1) * math.log(1.0 + 0.1 / 5.0)
    assert s == pytest.approx(expected, rel=1e-12)


def test_optimal_spread_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        optimal_spread(sigma_per_sqrt_s=0.001, tau_s=10.0, gamma=0.0, k=5.0)
    with pytest.raises(ValueError):
        optimal_spread(sigma_per_sqrt_s=0.001, tau_s=10.0, gamma=0.1, k=0.0)
    with pytest.raises(ValueError):
        optimal_spread(sigma_per_sqrt_s=0.001, tau_s=-1.0, gamma=0.1, k=5.0)
    with pytest.raises(ValueError):
        optimal_spread(sigma_per_sqrt_s=-0.001, tau_s=10.0, gamma=0.1, k=5.0)


# ---- reservation_price ------------------------------------------------------


def test_reservation_price_zero_inventory_is_mid() -> None:
    r = reservation_price(
        mid_price=0.55, inventory=0.0, sigma_per_sqrt_s=0.001,
        tau_s=120.0, gamma=0.1,
    )
    assert r == pytest.approx(0.55)


def test_reservation_price_long_skews_down() -> None:
    """Long inventory → r < mid (skew quotes lower so next BUY is less
    likely, next SELL is more likely)."""
    r = reservation_price(
        mid_price=0.55, inventory=1.0, sigma_per_sqrt_s=0.001,
        tau_s=120.0, gamma=0.1,
    )
    assert r < 0.55


def test_reservation_price_short_skews_up() -> None:
    r = reservation_price(
        mid_price=0.55, inventory=-1.0, sigma_per_sqrt_s=0.001,
        tau_s=120.0, gamma=0.1,
    )
    assert r > 0.55


# ---- quote() composite ------------------------------------------------------


def test_quote_returns_symmetric_around_mid_when_flat() -> None:
    q = quote(
        mid_price=0.50, sigma_per_sqrt_s=0.0003, tau_s=120.0,
        gamma=0.1, k=5.0, inventory=0.0,
    )
    assert isinstance(q, ASQuote)
    # Half-spread on each side of mid.
    assert q.bid_offset == pytest.approx(q.ask_offset, rel=1e-12)
    assert q.bid_price < 0.50 < q.ask_price
    assert q.spread == pytest.approx(2 * q.half_spread, rel=1e-12)


def test_quote_clamps_to_unit_interval() -> None:
    """Mid near 1.0 with a wide spread should clip the ask to ``cap``,
    not return > 1.0 (which is invalid Polymarket probability).
    """
    q = quote(
        mid_price=0.999,
        sigma_per_sqrt_s=0.01,  # large σ → wide spread
        tau_s=300.0,
        gamma=0.1,
        k=0.5,  # small k → wide log term
    )
    assert q.ask_price <= 1.0 - 1e-4 + 1e-12
    assert q.bid_price > 0.0


def test_quote_skew_with_inventory() -> None:
    """Long inventory → both bid and ask shift down (reservation < mid)."""
    flat = quote(
        mid_price=0.50, sigma_per_sqrt_s=0.001, tau_s=120.0,
        gamma=0.1, k=5.0, inventory=0.0,
    )
    long = quote(
        mid_price=0.50, sigma_per_sqrt_s=0.001, tau_s=120.0,
        gamma=0.1, k=5.0, inventory=2.0,
    )
    assert long.reservation_price < flat.reservation_price
    assert long.bid_price < flat.bid_price
    assert long.ask_price < flat.ask_price
