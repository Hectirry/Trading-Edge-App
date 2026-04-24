"""Multi-level Order-Flow Imbalance (Cont–Kukanov–Stoikov)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderBookLevel:
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float


def _side_flow(
    prev_price: float, prev_size: float, now_price: float, now_size: float, is_bid: bool
) -> float:
    """OFI contribution for a single side at one level.

    Bid: positive if bid improves or size grows at the same price;
    negative if bid worsens or shrinks. Ask is symmetric.
    """
    if is_bid:
        if now_price > prev_price:
            return now_size
        if now_price < prev_price:
            return -prev_size
        return now_size - prev_size
    # ask
    if now_price < prev_price:
        return now_size
    if now_price > prev_price:
        return -prev_size
    return now_size - prev_size


def mlofi(prev: Sequence[OrderBookLevel], now: Sequence[OrderBookLevel]) -> list[float]:
    """Per-level OFI: bid flow − ask flow.

    Both snapshots must have the same number of levels (the caller
    pads with zeros when the book is thin).
    """
    if len(prev) != len(now):
        raise ValueError("prev and now must share the same level count")
    out: list[float] = []
    for p, n in zip(prev, now, strict=True):
        bid = _side_flow(p.bid_price, p.bid_size, n.bid_price, n.bid_size, is_bid=True)
        ask = _side_flow(p.ask_price, p.ask_size, n.ask_price, n.ask_size, is_bid=False)
        out.append(bid - ask)
    return out
