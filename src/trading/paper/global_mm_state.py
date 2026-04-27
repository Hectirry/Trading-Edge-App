"""Process-wide MM state shared across MMPaperDriver instances.

Enforces global caps in `[paper.global_caps]` of staging.toml — applied
across all MM strategies (NOT direction-style ones). Pre-flight check in
`MMPaperDriver._dispatch_action`: a PostQuote/ReplaceQuote that would
push the aggregate over either cap is dropped, logged as
`mm_global_cap_drop`.

Aggregates tracked
------------------
- `total_capital_at_risk_usd`: sum across strategies of (open quotes
  notional + filled-not-settled notional).
- `total_inventory_usdc_across_strategies`: sum across strategies of
  `|inventory_shares × p_market|`.

Both are last-known-good approximations (updated on every fill / quote).
The cap check is conservative: it uses the cumulative across instances,
so any single instance acting out at its $50 inventory cap × 4 instances
= $200 ≤ global cap $250 by design.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from trading.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class GlobalMMState:
    max_total_capital_at_risk_usd: float = 1e9
    max_total_inventory_usdc: float = 1e9
    _inventory_usdc_by_strategy: dict[str, float] = field(default_factory=dict)
    _capital_at_risk_by_strategy: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def total_inventory_usdc(self) -> float:
        async with self._lock:
            return sum(self._inventory_usdc_by_strategy.values())

    async def total_capital_at_risk_usd(self) -> float:
        async with self._lock:
            return sum(self._capital_at_risk_by_strategy.values())

    async def update_strategy(
        self, strategy_id: str, inventory_usdc: float, capital_at_risk_usd: float
    ) -> None:
        async with self._lock:
            self._inventory_usdc_by_strategy[strategy_id] = inventory_usdc
            self._capital_at_risk_by_strategy[strategy_id] = capital_at_risk_usd

    async def block_post_quote(
        self,
        strategy_id: str,
        delta_inventory_usdc: float,
        delta_capital_at_risk_usd: float,
    ) -> tuple[bool, str]:
        """Pre-flight: would this quote breach a global cap?

        `delta_*` is the *additional* exposure this quote would add.
        Conservative: assumes any single quote could fill entirely.
        """
        async with self._lock:
            current_inv = self._inventory_usdc_by_strategy.get(strategy_id, 0.0)
            others_inv = sum(
                v for k, v in self._inventory_usdc_by_strategy.items() if k != strategy_id
            )
            projected_inv = others_inv + abs(current_inv) + delta_inventory_usdc
            if projected_inv > self.max_total_inventory_usdc:
                return True, (
                    f"global_inventory_cap: projected ${projected_inv:.2f} "
                    f"> cap ${self.max_total_inventory_usdc:.2f}"
                )

            current_cap = self._capital_at_risk_by_strategy.get(strategy_id, 0.0)
            others_cap = sum(
                v for k, v in self._capital_at_risk_by_strategy.items() if k != strategy_id
            )
            projected_cap = others_cap + current_cap + delta_capital_at_risk_usd
            if projected_cap > self.max_total_capital_at_risk_usd:
                return True, (
                    f"global_capital_cap: projected ${projected_cap:.2f} "
                    f"> cap ${self.max_total_capital_at_risk_usd:.2f}"
                )
            return False, "ok"
