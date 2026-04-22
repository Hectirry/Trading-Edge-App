"""PolymarketVenue mock — no network code, no API calls. See ADR 0004."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

POLYMARKET_VENUE = "POLYMARKET"


@dataclass(frozen=True)
class PolymarketInstrument:
    condition_id: str
    token_id: str
    slug: str
    close_ts: int
    side_label: str

    @property
    def instrument_id(self) -> str:
        return f"{self.slug}-{self.side_label}.{POLYMARKET_VENUE}"

    price_increment: Decimal = Decimal("0.01")
    size_increment: Decimal = Decimal("1")
    multiplier: Decimal = Decimal("1")
