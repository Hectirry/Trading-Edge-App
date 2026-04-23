"""Chainlink oracle ingestor (ADR 0012).

Polls whichever ``ChainlinkWatcher`` is active (``DataStreamsClient``
when the key is set, else ``EACProxyClient`` over Alchemy Polygon,
else no-op) and upserts every snapshot into
``market_data.chainlink_updates``. Strategies read the latest row
per feed at decision time instead of hitting the RPC in the hot path.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

from trading.common.config import get_settings
from trading.common.db import upsert_many
from trading.common.logging import get_logger
from trading.engine.features.chainlink_oracle import (
    NullChainlinkClient,
    pick_watcher,
)

log = get_logger(__name__)


async def run_chainlink_loop() -> None:
    settings = get_settings()
    watcher = pick_watcher(settings)
    if isinstance(watcher, NullChainlinkClient):
        log.warning("chainlink.disabled_no_keys")
        return
    interval_s = int(settings.chainlink_refresh_interval_s)
    log.info(
        "chainlink.loop.started",
        source=type(watcher).__name__,
        interval_s=interval_s,
    )
    while True:
        try:
            snap = await watcher.latest()
            if snap is not None:
                now_ts = time.time()
                row = (
                    datetime.fromtimestamp(now_ts, tz=UTC),
                    snap.feed,
                    int(snap.round_id),
                    Decimal(str(snap.answer)),
                    datetime.fromtimestamp(snap.updated_at_ts, tz=UTC),
                    float(max(0.0, now_ts - snap.updated_at_ts)),
                    snap.source,
                )
                await upsert_many(
                    "market_data.chainlink_updates",
                    ["ts", "feed", "round_id", "answer",
                     "updated_at", "age_s", "source"],
                    [row],
                    ["feed", "round_id"],
                )
        except Exception as e:
            log.warning("chainlink.loop.err", err=str(e))
        await asyncio.sleep(interval_s)
