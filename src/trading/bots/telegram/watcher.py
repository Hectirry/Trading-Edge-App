"""Heartbeat watcher + Phase 3 cron orchestrator living in
tea-telegram-bot. Fires Telegram alerts when the engine's heartbeat goes
stale, and schedules the daily + weekly jobs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import time

import redis.asyncio as redis

from trading.bots.telegram.commands import CommandPoller
from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger
from trading.notifications import telegram as T
from trading.paper.heartbeat import HEARTBEAT_KEY

log = get_logger("bots.telegram.watcher")

CHECK_INTERVAL_S = 30
LOST_THRESHOLD_S = 60


class Watcher:
    def __init__(self, redis_url: str) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=False)
        self.tg = T.TelegramClient()
        self._last_state_lost: bool = False

    async def run_heartbeat(self) -> None:
        log.info("watcher.heartbeat.started")
        while True:
            try:
                raw = await self._redis.get(HEARTBEAT_KEY)
                now = time()
                if raw is None:
                    age_s = None
                else:
                    data = json.loads(raw)
                    age_s = now - float(data.get("ts", 0))
                lost = raw is None or (age_s is not None and age_s > LOST_THRESHOLD_S)
                if lost and not self._last_state_lost:
                    await self.tg.send(T.heartbeat_lost(int(age_s or 9999)))
                elif not lost and self._last_state_lost:
                    await self.tg.send(T.heartbeat_recovered())
                self._last_state_lost = lost
            except Exception as e:
                log.warning("watcher.heartbeat.err", err=str(e))
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def run_daily_report(self) -> None:
        """Every 5 minutes, check wall clock; fire daily_report at 00:05 UTC."""
        log.info("watcher.daily_report.started")
        fired_for_day: str = ""
        while True:
            now = datetime.now(tz=UTC)
            # Fire once per day, at the first check after 00:05 UTC.
            today = now.strftime("%Y-%m-%d")
            if now.hour == 0 and now.minute >= 5 and fired_for_day != today:
                fired_for_day = today
                log.info("watcher.daily_report.firing", day=today)
                proc = await asyncio.create_subprocess_exec(
                    "python",
                    "-m",
                    "trading.cli.daily_report",
                )
                await proc.wait()
            await asyncio.sleep(60)

    async def run_weekly_comparison(self) -> None:
        """Every 5 min check; fire paper_vs_backtest on Sundays at 01:00 UTC."""
        log.info("watcher.weekly_comparison.started")
        fired_for_week: str = ""
        while True:
            now = datetime.now(tz=UTC)
            # weekday(): Monday=0 ... Sunday=6
            if now.weekday() == 6 and now.hour == 1 and now.minute >= 0:
                iso_week = now.strftime("%G-W%V")
                if fired_for_week != iso_week:
                    fired_for_week = iso_week
                    log.info("watcher.weekly_comparison.firing", week=iso_week)
                    proc = await asyncio.create_subprocess_exec(
                        "python",
                        "-m",
                        "trading.cli.paper_vs_backtest",
                    )
                    await proc.wait()
            await asyncio.sleep(60)


async def main_async() -> None:
    settings = get_settings()
    redis_url = f"redis://{settings.redis_host}:{settings.redis_port}/0"
    w = Watcher(redis_url)
    poller = CommandPoller()
    await asyncio.gather(
        w.run_heartbeat(),
        w.run_daily_report(),
        w.run_weekly_comparison(),
        poller.run(),
    )


def main() -> None:
    configure_logging()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
