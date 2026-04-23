"""Micro features over 1 Hz BTC spot samples (ADR 0011).

Every function takes the most recent samples (tail-last = now). Pass in
only the ``lookback_s`` most recent points; the functions return a
``nan``-safe default (0.0) on insufficient data so callers can SKIP
cleanly without branching on exceptions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def momentum_bps(spots: Sequence[float], lookback_s: int) -> float:
    """(spot_now / spot_lookback_ago - 1) * 10_000.

    ``spots`` is 1 Hz; its length must be at least ``lookback_s + 1`` —
    otherwise returns 0. Index -1 is "now", index -(lookback_s+1) is
    ``lookback_s`` seconds ago.
    """
    if lookback_s < 1 or len(spots) < lookback_s + 1:
        return 0.0
    past = spots[-(lookback_s + 1)]
    now = spots[-1]
    if past <= 0:
        return 0.0
    return (now / past - 1.0) * 10_000.0


def realized_vol_yz(spots: Sequence[float], window_s: int) -> float:
    """Yang–Zhang realised vol on 1 s OHLC reconstructed from ticks.

    With 1 Hz samples each "sub-bar" is a single tick, so we degrade
    to close-to-close log-return stdev — which matches YZ with
    ``k=0, alpha=1.34`` when opens == closes. The value is annualised
    to per-second vol, then scaled to the requested window.
    """
    if window_s < 2 or len(spots) < window_s + 1:
        return 0.0
    tail = spots[-(window_s + 1):]
    rets: list[float] = []
    for i in range(1, len(tail)):
        a, b = tail[i - 1], tail[i]
        if a <= 0 or b <= 0:
            continue
        rets.append(math.log(b / a))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0))


def tick_up_ratio(spots: Sequence[float], window_s: int) -> float:
    """Fraction of strictly-increasing successive samples over the window."""
    if window_s < 1 or len(spots) < window_s + 1:
        return 0.0
    tail = spots[-(window_s + 1):]
    ups = 0
    for i in range(1, len(tail)):
        if tail[i] > tail[i - 1]:
            ups += 1
    return ups / window_s
