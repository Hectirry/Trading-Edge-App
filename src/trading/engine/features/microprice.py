"""Stoikov microprice for a binary Polymarket YES/NO book."""

from __future__ import annotations


def microprice(
    yes_ask: float, no_ask: float, depth_yes: float, depth_no: float
) -> float:
    """Depth-weighted fair probability of YES.

    yes_ask + no_ask ≈ 1.0 in a healthy book; we interpret yes_ask as
    the "buy YES" price and ``1 - no_ask`` as the "sell YES" price and
    depth-weight between them.
    """
    total = max(depth_yes + depth_no, 1e-12)
    yes_mid = yes_ask
    sell_yes = 1.0 - no_ask
    # Weighted towards the side with the most depth: more depth ⇒ that
    # side anchors the fair.
    return (depth_no * yes_mid + depth_yes * sell_yes) / total
