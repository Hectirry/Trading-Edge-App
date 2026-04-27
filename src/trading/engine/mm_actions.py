"""MM action sum type — used by `StrategyBase.on_tick` for continuous quoting.

Distinct from `Action` enum (ENTER/SKIP) in `types.py` which serves the
one-shot direction-style strategies. mm_rebate_v1 (and any future MM
strategy) returns `list[MMAction]` from `on_tick`; the paper / backtest
driver dispatches each action against `limit_book_sim`.

All dataclasses are frozen so that an action emitted in one tick cannot
be mutated downstream by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from trading.engine.types import Side


@dataclass(frozen=True)
class PostQuote:
    """Place a new GTC limit quote at `price` for `qty_shares` on `side`.

    `client_id_seed` is a strategy-supplied stable suffix that the driver
    folds into the deterministic client_order_id (sha256(strategy|slug|ts|
    side|seed)[:16]). Use it when posting multiple quotes in the same tick
    on the same side to keep IDs distinct.
    """

    side: Side                    # YES_UP = bid book buy / YES_DOWN = bid book sell
    price: float                  # YES-price units
    qty_shares: float
    market_slug: str
    ttl_seconds: int = 0          # 0 = GTC, else cancel after ttl_seconds
    client_id_seed: str = ""      # disambiguator within a tick


@dataclass(frozen=True)
class CancelQuote:
    """Cancel an existing resting quote by client_order_id."""

    client_order_id: str
    market_slug: str
    reason: str = ""


@dataclass(frozen=True)
class ReplaceQuote:
    """Atomic cancel-and-place. Driver should attempt cancel first;
    if cancel succeeds, place the new quote with the new params.
    """

    old_client_order_id: str
    new: PostQuote


# Type alias used by StrategyBase.on_tick return type and driver dispatch.
MMAction = Union[PostQuote, CancelQuote, ReplaceQuote]
