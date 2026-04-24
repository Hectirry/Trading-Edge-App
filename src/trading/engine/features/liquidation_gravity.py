"""Liquidation-cluster gravity signal (ADR 0012).

Stateless: reads a list of ``LiqCluster`` objects (normally pulled
from ``market_data.liquidation_clusters``) and scores proximity to
clusters within ±band_pct of spot.

Two outputs:
- ``gravity_down`` — expected pull toward clusters BELOW spot (long
  liquidations cascade bias). Higher = more downside gravity.
- ``gravity_up`` — symmetric above-spot short-liquidation pull.

The raw ``(down, up)`` pair is later combined into a single signed
score by the strategy (e.g. up-down).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LiqCluster:
    ts: float
    side: str  # 'long' | 'short'
    price: float
    size_usd: float


def _proximity_weight(dist_pct: float, band_pct: float) -> float:
    """1.0 at zero distance, 0.0 at ±band, linear in between."""
    if dist_pct >= band_pct:
        return 0.0
    return max(0.0, 1.0 - (dist_pct / band_pct))


def gravity_scores(
    spot: float,
    clusters: Sequence[LiqCluster],
    *,
    band_pct: float = 0.3,
    close_band_pct: float = 0.15,
    large_cluster_usd: float = 100_000.0,
) -> tuple[float, float]:
    """Return ``(gravity_down, gravity_up)`` ∈ [0, 1].

    Within ``close_band_pct`` (0.15 %) a single cluster ≥ ``large_cluster_usd``
    is enough to saturate its side's score. Beyond that, sum of
    proximity-weighted sizes normalised to 1.
    """
    if spot <= 0 or not clusters:
        return 0.0, 0.0

    down_raw = 0.0
    up_raw = 0.0
    max_mass = max(large_cluster_usd, 1.0)

    for c in clusters:
        if c.price <= 0 or c.size_usd <= 0:
            continue
        dist_pct = abs(c.price - spot) / spot * 100.0
        w = _proximity_weight(dist_pct, band_pct)
        if w <= 0:
            continue
        is_close = dist_pct <= close_band_pct
        is_large = c.size_usd >= large_cluster_usd
        mass = c.size_usd * w
        if is_close and is_large:
            mass = max_mass  # saturate immediately
        if c.price < spot:
            down_raw += mass
        else:
            up_raw += mass

    down = min(1.0, down_raw / max_mass)
    up = min(1.0, up_raw / max_mass)
    return down, up


def signed_gravity(down: float, up: float) -> float:
    """Compact signed magnitude ∈ [-1, 1]. Positive = up-side pull."""
    return max(-1.0, min(1.0, up - down))
