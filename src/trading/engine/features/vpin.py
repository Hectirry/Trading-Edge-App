"""VPIN (Volume-Synchronized Probability of Informed Trading).

Implementation follows López de Prado & Easley. Trades arrive as
(ts, qty_usd, side) where ``side`` ∈ {"BUY", "SELL"}. We bucket by
volume — each bucket accumulates ``bucket_volume`` USD — then compute
``|sum(BUY) - sum(SELL)| / bucket_volume`` per bucket and average over
the last ``n_buckets``.
"""

from __future__ import annotations

from collections.abc import Sequence


def vpin(
    trades: Sequence[tuple[float, float, str]],
    bucket_volume: float,
    n_buckets: int = 50,
) -> float:
    if bucket_volume <= 0 or n_buckets < 1 or not trades:
        return 0.0

    buy_bucket = 0.0
    sell_bucket = 0.0
    bucket_fill = 0.0
    bucket_imbalances: list[float] = []

    for _ts, qty, side in trades:
        if qty <= 0:
            continue
        remaining = qty
        is_buy = side.upper() == "BUY"
        while remaining > 0:
            room = bucket_volume - bucket_fill
            chunk = min(room, remaining)
            if is_buy:
                buy_bucket += chunk
            else:
                sell_bucket += chunk
            bucket_fill += chunk
            remaining -= chunk
            if bucket_fill >= bucket_volume - 1e-9:
                bucket_imbalances.append(
                    abs(buy_bucket - sell_bucket) / bucket_volume
                )
                buy_bucket = 0.0
                sell_bucket = 0.0
                bucket_fill = 0.0

    if not bucket_imbalances:
        return 0.0
    tail = bucket_imbalances[-n_buckets:]
    return sum(tail) / len(tail)
