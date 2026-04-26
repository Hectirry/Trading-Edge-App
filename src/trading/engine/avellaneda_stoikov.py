"""Avellaneda-Stoikov optimal market-making spread / quotes.

Reference: Avellaneda & Stoikov (2008) "*High-frequency trading in a
limit order book*", Quantitative Finance 8(3), 217-224.

Used by ``oracle_lag_v2`` (ADR 0014) to compute the optimal quote
distance from the *reservation price* on a Polymarket binary outcome
when posting maker-side limit orders. The closed-form for symmetric
quotes around the reservation price ``r`` is:

::

    r       = s − q · γ · σ² · (T − t)            # reservation price
    spread  = γ · σ² · (T − t) + (2/γ) · ln(1 + γ/k)
    δ_a     = (r − s) + spread / 2                 # ask offset from mid
    δ_b     = (s − r) + spread / 2                 # bid offset from mid

Where:

- ``s`` = mid price (here: Polymarket implied prob of the favoured outcome).
- ``q`` = current inventory (signed; long-yes positive, long-no negative).
- ``γ`` = inventory aversion (default 0.1; higher = more skew when long).
- ``σ`` = price volatility per √second (same units as Black-Scholes form).
- ``T - t`` = seconds until window close (= τ in the BS pricer).
- ``k`` = order arrival rate parameter — tunes the trade-off between
  the rebate captured (wide spread) and fill probability (narrow
  spread). Default k=5 is a Polymarket-quiet-market estimate; faster
  markets push k higher and the optimal spread tighter.

For ``oracle_lag_v2`` at session-start (Sprint D) we run with ``q = 0``
(one trade per market window, no inventory carry-over). The reservation
price collapses to ``r = s`` and the buy/sell offsets are symmetric at
``spread / 2``. The full asymmetric form is preserved here so that the
same module can be reused if v2 ever runs multi-fill / inventory-aware.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ASQuote:
    """A pair of optimal maker quotes around the mid.

    All offsets are in the SAME UNITS as ``mid_price``. For Polymarket
    binary outcomes those are probability units (∈ [0, 1]).

    - ``reservation_price``: the inventory-adjusted center the spread
      is built around. Equals ``mid_price`` when ``q = 0``.
    - ``half_spread``: ½ · spread*. Symmetric distance from the
      reservation price. Always ≥ 0.
    - ``bid_price``, ``ask_price``: clipped to ``(0, 1)`` so the
      caller can post directly without re-clamping. The strategy
      should still validate against the live book (don't post a buy
      above the current ask).
    """

    reservation_price: float
    half_spread: float
    bid_price: float
    ask_price: float
    bid_offset: float  # signed; mid - bid (≥ 0 when q ≤ 0)
    ask_offset: float  # signed; ask - mid (≥ 0 when q ≥ 0)

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price


def optimal_spread(
    *,
    sigma_per_sqrt_s: float,
    tau_s: float,
    gamma: float,
    k: float,
) -> float:
    """Closed-form Avellaneda-Stoikov optimal spread.

        spread* = γ · σ² · (T - t) + (2 / γ) · ln(1 + γ / k)

    All inputs must be > 0. ``tau_s`` may be 0 (collapses the inventory
    term — only the asymptotic rebate-vs-fill log term survives).
    Returns the *full* spread (ask − bid), not the half-spread.

    Edge cases:
    - ``tau_s == 0``: only the log term contributes (asymptote).
    - ``sigma_per_sqrt_s == 0``: degenerate vol; only log term.
    - ``gamma → 0``: spread → ∞ (perfectly risk-neutral makers
      push quotes far enough that they always rebate). Guard with
      ``gamma > 0``.
    - ``k → 0``: arrival rate vanishes; spread → ∞.
    """
    if gamma <= 0:
        raise ValueError(f"gamma must be > 0, got {gamma}")
    if k <= 0:
        raise ValueError(f"k must be > 0, got {k}")
    if tau_s < 0:
        raise ValueError(f"tau_s must be ≥ 0, got {tau_s}")
    if sigma_per_sqrt_s < 0:
        raise ValueError(f"sigma must be ≥ 0, got {sigma_per_sqrt_s}")

    inventory_term = gamma * sigma_per_sqrt_s * sigma_per_sqrt_s * tau_s
    log_term = (2.0 / gamma) * math.log(1.0 + gamma / k)
    return inventory_term + log_term


def reservation_price(
    *,
    mid_price: float,
    inventory: float,
    sigma_per_sqrt_s: float,
    tau_s: float,
    gamma: float,
) -> float:
    """Inventory-adjusted reservation price.

        r = s − q · γ · σ² · (T - t)

    With ``q = 0`` this is just the mid. Sign convention: long → r < s
    (skew quotes lower so the next sell is more likely; the next buy is
    less likely). Short → r > s.
    """
    if gamma <= 0:
        raise ValueError(f"gamma must be > 0, got {gamma}")
    if tau_s < 0:
        raise ValueError(f"tau_s must be ≥ 0, got {tau_s}")
    return mid_price - inventory * gamma * sigma_per_sqrt_s * sigma_per_sqrt_s * tau_s


def quote(
    *,
    mid_price: float,
    sigma_per_sqrt_s: float,
    tau_s: float,
    gamma: float = 0.1,
    k: float = 5.0,
    inventory: float = 0.0,
    floor: float = 1e-4,
    cap: float = 1.0 - 1e-4,
) -> ASQuote:
    """Return Avellaneda-Stoikov optimal bid/ask around ``mid_price``.

    Defaults match the ADR 0014 blueprint: γ = 0.1, k = 5. ``floor``
    and ``cap`` clamp the output prices into the valid Polymarket
    probability interval (we reject 0 and 1 because those mean the
    book has resolved). Pass ``floor=0, cap=float('inf')`` if using
    this on a non-binary instrument.

    The returned object exposes both the absolute prices and the
    signed offsets from the mid so callers can rate-limit on
    ``offset`` thresholds (e.g. "only re-quote if mid moved more than
    half_spread").
    """
    spread = optimal_spread(
        sigma_per_sqrt_s=sigma_per_sqrt_s, tau_s=tau_s, gamma=gamma, k=k
    )
    half = spread / 2.0
    r = reservation_price(
        mid_price=mid_price,
        inventory=inventory,
        sigma_per_sqrt_s=sigma_per_sqrt_s,
        tau_s=tau_s,
        gamma=gamma,
    )
    raw_bid = r - half
    raw_ask = r + half
    bid = max(floor, min(cap, raw_bid))
    ask = max(floor, min(cap, raw_ask))
    return ASQuote(
        reservation_price=r,
        half_spread=half,
        bid_price=bid,
        ask_price=ask,
        bid_offset=mid_price - bid,
        ask_offset=ask - mid_price,
    )


__all__ = [
    "ASQuote",
    "optimal_spread",
    "quote",
    "reservation_price",
]
