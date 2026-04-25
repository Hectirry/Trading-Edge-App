"""Sync microstructure provider for the v3 strategy serving path.

The strategy's ``should_enter`` is a sync method but the canonical
microstructure feature builder is async (queries
``market_data.crypto_trades``). To bridge the gap we keep an in-memory
cache of the last computed feature dict, refreshed on a 5 s cadence
from the engine's shared-providers loop.

Train/serve invariant: this provider hands back exactly the dict
produced by ``binance_microstructure_features`` — same code path used
in training. Staleness > ``max_staleness_s`` flips the dict to
``_empty_features()`` (sentinels) so the strategy never serves on a
silently-stale cache.
"""

from __future__ import annotations

import time

from trading.common.logging import get_logger
from trading.engine.features.binance_microstructure import (
    _empty_features,
    binance_microstructure_features,
)

log = get_logger("strategy.microstructure_provider")


class PostgresMicrostructureProvider:
    def __init__(
        self,
        *,
        window_s: int = 90,
        large_threshold_usd: float = 100_000.0,
        max_staleness_s: float = 30.0,
    ) -> None:
        self.window_s = window_s
        self.large_threshold_usd = large_threshold_usd
        self.max_staleness_s = max_staleness_s
        self._cache: dict[str, float] = _empty_features()
        self._cache_ts: float = 0.0

    async def refresh(self) -> None:
        from trading.common.db import acquire

        now = time.time()
        try:
            async with acquire() as conn:
                feats = await binance_microstructure_features(
                    now,
                    window_s=self.window_s,
                    large_threshold_usd=self.large_threshold_usd,
                    conn=conn,
                )
            self._cache = feats
            self._cache_ts = now
        except Exception as e:
            log.warning("microstructure.refresh_err", err=str(e))

    def fetch(self, ts: float) -> dict[str, float]:
        if self._cache_ts <= 0:
            return _empty_features()
        if abs(ts - self._cache_ts) > self.max_staleness_s:
            log.warning(
                "microstructure.stale",
                ts=ts,
                cache_ts=self._cache_ts,
                age_s=abs(ts - self._cache_ts),
            )
            return _empty_features()
        return self._cache
