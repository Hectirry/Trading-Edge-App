"""Runtime data providers shared by ``contest_ensemble_v1`` /
``contest_avengers_v1`` (ADR 0012).

Design: periodic async ``refresh()`` (called by a paper_engine task),
instantaneous sync ``snapshot()`` (called by ``should_enter``). Keeps
``StrategyBase.should_enter`` sync so the driver + all existing
strategies don't change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from trading.common.db import acquire
from trading.common.logging import get_logger
from trading.engine.features.liquidation_gravity import LiqCluster

log = get_logger(__name__)


@dataclass
class CachedLiqClusters:
    symbol: str = "BTCUSDT_PERP.A"
    _rows: list[LiqCluster] = field(default_factory=list)
    _last_refresh_ts: float = 0.0

    async def refresh(self) -> None:
        try:
            async with acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ts, side, price, size_usd "
                    "FROM market_data.liquidation_clusters "
                    "WHERE symbol = $1 AND ts > now() - interval '1 hour' "
                    "ORDER BY ts DESC LIMIT 500",
                    self.symbol,
                )
        except Exception as e:
            log.warning("liq_clusters.fetch_err", err=str(e))
            return
        self._rows = [
            LiqCluster(
                ts=r["ts"].timestamp(),
                side=str(r["side"]),
                price=float(r["price"]),
                size_usd=float(r["size_usd"]),
            )
            for r in rows
        ]
        self._last_refresh_ts = time.time()

    def snapshot(self) -> list[LiqCluster]:
        return list(self._rows)

    @property
    def fresh_age_s(self) -> float:
        return time.time() - self._last_refresh_ts if self._last_refresh_ts else 9999.0


@dataclass
class CachedChainlinkSnapshot:
    _row: dict | None = None
    _last_refresh_ts: float = 0.0

    async def refresh(self) -> None:
        try:
            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT answer, updated_at, age_s, source "
                    "FROM market_data.chainlink_updates "
                    "ORDER BY ts DESC LIMIT 1"
                )
        except Exception as e:
            log.warning("chainlink_cache.fetch_err", err=str(e))
            return
        if row is None:
            return
        self._row = {
            "answer": float(row["answer"]),
            "updated_at_ts": float(row["updated_at"].timestamp()),
            "source": str(row["source"]),
        }
        self._last_refresh_ts = time.time()

    def snapshot(self) -> dict | None:
        if self._row is None:
            return None
        # Re-compute lag at read time so stale rows still reflect actual age.
        age_s = max(0.0, time.time() - self._row["updated_at_ts"])
        return {**self._row, "age_s": age_s}

    @property
    def fresh_age_s(self) -> float:
        return time.time() - self._last_refresh_ts if self._last_refresh_ts else 9999.0
