"""TickRecorder: emits a TickContext per open market at 1 Hz (master clock
= Binance 1s kline). Writes to `market_data.paper_ticks` and publishes
serialized ticks on Redis channel `tea:paper_ticks`.

The driver consumes from Redis; the DB write preserves full history for
the weekly paper-vs-backtest replay.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from time import time

import orjson
import redis.asyncio as redis

from trading.common.db import upsert_many
from trading.common.logging import get_logger
from trading.paper.feeds import compute_derived_book
from trading.paper.state import FeedState

log = get_logger(__name__)

REDIS_CHANNEL = "tea:paper_ticks"


class TickRecorder:
    def __init__(self, state: FeedState, redis_url: str) -> None:
        self.state = state
        self.redis_url = redis_url
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=False)
        return self._redis

    async def run(self) -> None:
        r = await self._get_redis()
        log.info("paper.tick_recorder.started")
        last_spot_ts = 0.0
        while True:
            # Wait for a new spot tick (~1 Hz).
            if self.state.spot_ts <= last_spot_ts:
                await asyncio.sleep(0.1)
                continue
            last_spot_ts = self.state.spot_ts
            now = time()
            async with self.state.lock:
                open_markets = [m for m in self.state.markets.values() if m.window_close_ts > now]
                feed_snapshot = {
                    "spot": self.state.spot_price,
                    "chainlink": self.state.chainlink_price,
                }
                books = {
                    cid: self.state.books.get(cid) for cid in (m.condition_id for m in open_markets)
                }

            db_rows: list[tuple] = []
            published = 0
            for m in open_markets:
                book = books.get(m.condition_id)
                if not book:
                    continue
                # Capture open_price at first tick within 5 s of window_open.
                # If we miss the window (process restart, late discovery,
                # feed interruption), DO NOT retrofit from a mid-window
                # Chainlink reading — that creates a false "open" that
                # flips went_up in the settle watchdog. Leave open_price
                # at 0 and let the watchdog's ohlcv fallback handle it.
                window_open = m.window_close_ts - 300
                t_in_win = max(0.0, now - window_open)
                if not m.open_price_captured and now >= window_open and t_in_win <= 5.0:
                    # Prefer spot (continuous Binance 1Hz) for the open
                    # snapshot; Chainlink freezes too often on Polygon
                    # EAC to be trusted as the window-open reference.
                    m.open_price = feed_snapshot["spot"] or feed_snapshot["chainlink"]
                    m.open_price_captured = True

                t_in_window = max(0.0, now - window_open)
                if t_in_window > 300:
                    continue
                derived = compute_derived_book(book)
                tick = {
                    "ts": now,
                    "condition_id": m.condition_id,
                    "market_slug": m.slug,
                    "t_in_window": t_in_window,
                    "window_close_ts": m.window_close_ts,
                    "spot_price": feed_snapshot["spot"],
                    "chainlink_price": feed_snapshot["chainlink"],
                    "open_price": m.open_price,
                    "pm_yes_bid": book.yes_bid,
                    "pm_yes_ask": book.yes_ask,
                    "pm_no_bid": book.no_bid,
                    "pm_no_ask": book.no_ask,
                    "pm_depth_yes": book.depth_yes,
                    "pm_depth_no": book.depth_no,
                    "pm_imbalance": derived["imbalance"] or 0.0,
                    "pm_spread_bps": derived["spread_bps"] or 0.0,
                    "implied_prob_yes": derived["implied_prob_yes"] or 0.0,
                }
                # Redis publish (raw JSON for downstream driver).
                try:
                    await r.publish(REDIS_CHANNEL, orjson.dumps(tick))
                    published += 1
                except Exception as e:
                    log.warning("paper.tick_recorder.publish_err", err=str(e))

                db_rows.append(
                    (
                        _ts_to_datetime(now),
                        m.condition_id,
                        m.slug,
                        t_in_window,
                        m.window_close_ts,
                        _d(feed_snapshot["spot"]),
                        _d(feed_snapshot["chainlink"]),
                        _d(m.open_price),
                        _d(book.yes_bid),
                        _d(book.yes_ask),
                        _d(book.no_bid),
                        _d(book.no_ask),
                        _d(book.depth_yes),
                        _d(book.depth_no),
                        _d(derived["imbalance"] or 0.0),
                        derived["spread_bps"] or 0.0,
                        _d(derived["implied_prob_yes"] or 0.0),
                    )
                )

            if db_rows:
                try:
                    await upsert_many(
                        "market_data.paper_ticks",
                        [
                            "ts",
                            "condition_id",
                            "market_slug",
                            "t_in_window",
                            "window_close_ts",
                            "spot_price",
                            "chainlink_price",
                            "open_price",
                            "pm_yes_bid",
                            "pm_yes_ask",
                            "pm_no_bid",
                            "pm_no_ask",
                            "pm_depth_yes",
                            "pm_depth_no",
                            "pm_imbalance",
                            "pm_spread_bps",
                            "implied_prob_yes",
                        ],
                        db_rows,
                        ["condition_id", "ts"],
                    )
                except Exception as e:
                    log.warning("paper.tick_recorder.db_err", err=str(e))

            if published and published % 30 == 0:
                log.info(
                    "paper.tick_recorder.heartbeat",
                    open_markets=len(open_markets),
                    published=published,
                )


def _ts_to_datetime(ts: float):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC)


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def parse_tick(raw: bytes) -> dict:
    return json.loads(raw)
