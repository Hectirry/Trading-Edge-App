"""Macro snapshot provider — read-only access to 5m Binance candles.

Two variants:

- :class:`PostgresMacroProvider` queries ``market_data.crypto_ohlcv``
  and caches the last 40 candles in memory; used by the paper driver.
  The cache is refreshed on demand (``snapshot_at`` falls through to a
  DB fetch when the requested ``as_of_ts`` is newer than the cached
  tail).
- :class:`FixedMacroProvider` takes a pre-built list of candles in
  memory; used by backtests and golden-trace tests for deterministic
  output.

Both implement the minimal ``snapshot_at(as_of_ts) -> MacroSnapshot |
None`` interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading.engine.features.macro import MacroSnapshot, snapshot


@dataclass
class Candle:
    ts: float
    high: float
    low: float
    close: float


class FixedMacroProvider:
    """In-memory provider used by backtest + tests.

    ``candles`` must be sorted ascending by ``ts``. ``snapshot_at``
    returns the MacroSnapshot built from the last 34 candles with
    ``ts + 300 <= as_of_ts`` (i.e. closed-bar only, no leak).
    """

    def __init__(
        self,
        candles: list[Candle],
        *,
        lookback: int = 34,
        adx_threshold: float = 20.0,
        consecutive_min: int = 2,
    ) -> None:
        self.candles = sorted(candles, key=lambda c: c.ts)
        self.lookback = lookback
        self.adx_threshold = adx_threshold
        self.consecutive_min = consecutive_min

    def snapshot_at(self, as_of_ts: float) -> MacroSnapshot | None:
        cutoff = as_of_ts - 300.0
        eligible = [c for c in self.candles if c.ts <= cutoff]
        if len(eligible) < self.lookback:
            return None
        window = eligible[-self.lookback :]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        closes = [c.close for c in window]
        return snapshot(
            highs,
            lows,
            closes,
            adx_threshold=self.adx_threshold,
            consecutive_min=self.consecutive_min,
        )


class PostgresMacroProvider:
    """Live provider that pulls from ``market_data.crypto_ohlcv``.

    Holds a cached tail of candles and refreshes when the requested
    ``as_of_ts`` crosses the cache boundary. Caller must ``await
    provider.refresh()`` before first use.
    """

    def __init__(
        self,
        *,
        symbol: str = "BTCUSDT",
        exchange: str = "binance",
        interval: str = "5m",
        lookback: int = 34,
        adx_threshold: float = 20.0,
        consecutive_min: int = 2,
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.interval = interval
        self.lookback = lookback
        self.adx_threshold = adx_threshold
        self.consecutive_min = consecutive_min
        self._cache: list[Candle] = []

    async def refresh(self, hours: int = 6) -> None:
        from trading.common.db import acquire

        since = datetime.fromtimestamp(
            datetime.now(tz=UTC).timestamp() - hours * 3600,
            tz=UTC,
        )
        async with acquire() as conn:
            rows = await conn.fetch(
                "SELECT ts, high, low, close FROM market_data.crypto_ohlcv "
                "WHERE exchange=$1 AND symbol=$2 AND interval=$3 AND ts > $4 "
                "ORDER BY ts ASC",
                self.exchange,
                self.symbol,
                self.interval,
                since,
            )
        self._cache = [
            Candle(
                ts=r["ts"].timestamp(),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
            )
            for r in rows
        ]

    def snapshot_at(self, as_of_ts: float) -> MacroSnapshot | None:
        cutoff = as_of_ts - 300.0
        eligible = [c for c in self._cache if c.ts <= cutoff]
        if len(eligible) < self.lookback:
            return None
        window = eligible[-self.lookback :]
        return snapshot(
            [c.high for c in window],
            [c.low for c in window],
            [c.close for c in window],
            adx_threshold=self.adx_threshold,
            consecutive_min=self.consecutive_min,
        )
