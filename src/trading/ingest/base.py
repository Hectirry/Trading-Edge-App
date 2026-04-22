from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class IngestError(Exception):
    pass


class IngestRateLimitError(IngestError):
    pass


class IngestSourceDown(IngestError):
    pass


class IngestDataIntegrityError(IngestError):
    pass


@dataclass
class HealthStatus:
    alive: bool
    last_message_ts: datetime | None
    last_error: str | None
    messages_per_min: float


class CryptoIngestAdapter(ABC):
    name: str

    @abstractmethod
    async def backfill_ohlcv(
        self,
        symbol: str,
        interval: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> int: ...

    @abstractmethod
    async def backfill_trades(
        self,
        symbol: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> int: ...

    @abstractmethod
    async def stream_ohlcv(self, symbols: list[str], intervals: list[str]) -> None: ...

    @abstractmethod
    async def stream_trades(self, symbols: list[str]) -> None: ...

    @abstractmethod
    def health(self) -> HealthStatus: ...


class PolymarketIngestAdapter(ABC):
    name: str

    @abstractmethod
    async def discover_markets(self, slug_pattern: str, since: datetime) -> int: ...

    @abstractmethod
    async def backfill_market_prices(self, condition_id: str) -> int: ...

    @abstractmethod
    async def stream_prices(self, condition_ids: list[str]) -> None: ...

    @abstractmethod
    def health(self) -> HealthStatus: ...
