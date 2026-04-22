"""Shared in-memory state for the Phase 3 paper engine.

Three WebSocket tasks (Binance, Polymarket CLOB, Chainlink RTDS) write
into this object; one master-clock task (Binance 1s kline) reads it to
compose a TickContext per open market and publish to Redis + write to
`market_data.paper_ticks`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import time


@dataclass
class CLOBLevel:
    price: float
    size: float


@dataclass
class CLOBBookSnapshot:
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    depth_yes: float = 0.0  # total USD on YES side
    depth_no: float = 0.0
    last_update_ts: float = 0.0


@dataclass
class MarketMeta:
    condition_id: str
    slug: str
    yes_token_id: str
    no_token_id: str
    window_close_ts: int
    open_price: float = 0.0  # captured at window open
    open_price_captured: bool = False


@dataclass
class FeedState:
    spot_price: float = 0.0
    spot_ts: float = 0.0
    chainlink_price: float = 0.0
    chainlink_ts: float = 0.0

    # condition_id -> book snapshot.
    books: dict[str, CLOBBookSnapshot] = field(default_factory=dict)
    # condition_id -> market meta.
    markets: dict[str, MarketMeta] = field(default_factory=dict)
    # token_id -> condition_id for fast CLOB dispatch.
    token_to_condition: dict[str, str] = field(default_factory=dict)
    # token_id -> "yes" | "no"
    token_side: dict[str, str] = field(default_factory=dict)

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def feeds_fresh(self, max_age_s: float = 10.0) -> dict[str, bool]:
        now = time()
        return {
            "spot": now - self.spot_ts < max_age_s and self.spot_price > 0,
            "chainlink": now - self.chainlink_ts < max_age_s and self.chainlink_price > 0,
        }
