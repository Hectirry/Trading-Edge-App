"""Engine heartbeat publisher.

Writes to Redis key `tea:engine:last_heartbeat` (TTL 120 s) every
HEARTBEAT_INTERVAL_S and publishes on the channel of the same name for
downstream watchers that prefer pub/sub semantics.
"""

from __future__ import annotations

import asyncio
import json
from time import time

import redis.asyncio as redis

from trading.common.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_KEY = "tea:engine:last_heartbeat"
HEARTBEAT_CHANNEL = "tea:engine:heartbeat"


class HeartbeatPublisher:
    def __init__(self, redis_url: str, interval_s: int = 10) -> None:
        self.redis_url = redis_url
        self.interval_s = interval_s
        self._redis: redis.Redis | None = None
        self._startup_ts = time()
        self.n_trades_today = 0
        self.n_open_positions = 0

    async def run(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=False)
        log.info("paper.heartbeat.publisher.started", interval_s=self.interval_s)
        while True:
            payload = {
                "ts": time(),
                "uptime_s": int(time() - self._startup_ts),
                "n_trades_today": self.n_trades_today,
                "n_open_positions": self.n_open_positions,
            }
            raw = json.dumps(payload).encode()
            try:
                await self._redis.set(HEARTBEAT_KEY, raw, ex=120)
                await self._redis.publish(HEARTBEAT_CHANNEL, raw)
            except Exception as e:
                log.warning("paper.heartbeat.publish_err", err=str(e))
            await asyncio.sleep(self.interval_s)
