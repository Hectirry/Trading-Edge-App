"""Heartbeat watcher + Phase 3 cron orchestrator living in
tea-telegram-bot. Fires Telegram alerts when the engine's heartbeat goes
stale, and schedules the daily + weekly jobs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import time

import redis.asyncio as redis

from trading.bots.telegram import tools as control_tools
from trading.bots.telegram.api_client import TradingAPIClient
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
        self.settings = get_settings()
        self._redis = redis.from_url(redis_url, decode_responses=False)
        self.tg = T.TelegramClient()
        self._last_state_lost: bool = False
        self._control_api = TradingAPIClient(
            base_url=self.settings.api_base_url,
            api_token=self.settings.api_token,
        )
        self._engine_alert_active = False
        self._killswitch_alert_active = False
        self._stale_trades_alert_active = False
        self._pnl_alert_day = ""

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
        """Every 5 min check; fire paper_vs_backtest on Sundays at 01:00 UTC
        and the contest A/B digest on Sundays at 12:00 UTC.
        """
        log.info("watcher.weekly_comparison.started")
        fired_for_week_pvb: str = ""
        fired_for_week_ab: str = ""
        while True:
            now = datetime.now(tz=UTC)
            # weekday(): Monday=0 ... Sunday=6
            iso_week = now.strftime("%G-W%V")
            if now.weekday() == 6 and now.hour == 1 and fired_for_week_pvb != iso_week:
                fired_for_week_pvb = iso_week
                log.info("watcher.weekly_comparison.firing", week=iso_week)
                proc = await asyncio.create_subprocess_exec(
                    "python",
                    "-m",
                    "trading.cli.paper_vs_backtest",
                )
                await proc.wait()
            if now.weekday() == 6 and now.hour == 12 and fired_for_week_ab != iso_week:
                fired_for_week_ab = iso_week
                log.info("watcher.contest_ab.firing", week=iso_week)
                proc = await asyncio.create_subprocess_exec(
                    "python",
                    "-m",
                    "trading.cli.contest_ab_weekly",
                )
                await proc.wait()
            await asyncio.sleep(60)

    async def run_walk_forward_sunday(self) -> None:
        """Fire per-strategy walk-forward runs every Sunday at 02:00 UTC.

        Reports only — promotion is manual (ADR 0011/0012). A Sunday
        cron covers the week's fresh paper data + avoids colliding with
        the Sunday 01:00 paper_vs_backtest job. One strategy per
        minute to spread load across the hour.
        """
        log.info("watcher.walk_forward_sunday.started")
        fired_for_week: str = ""
        strategies = [
            "hmm_regime_btc5m",
            "last_90s_forecaster_v2",
            "contest_ensemble_v1",
            "imbalance_v3",
            "trend_confirm_t1_v1",
            "last_90s_forecaster_v1",
            "contest_avengers_v1",
        ]
        while True:
            now = datetime.now(tz=UTC)
            iso_week = now.strftime("%G-W%V")
            if now.weekday() == 6 and now.hour == 2 and fired_for_week != iso_week:
                fired_for_week = iso_week
                log.info("watcher.walk_forward_sunday.firing", week=iso_week)
                t_to = now.replace(hour=0, minute=0, second=0, microsecond=0)
                t_from = t_to - timedelta(days=30)
                for i, strategy in enumerate(strategies):
                    # Stagger start by strategy index × 60 s so the
                    # tea-engine CPU isn't pegged by 7 Optuna runs at
                    # once.
                    if i > 0:
                        await asyncio.sleep(60)
                    argv = [
                        "python",
                        "-m",
                        "trading.cli.walk_forward",
                        "--strategy",
                        strategy,
                        "--from",
                        t_from.date().isoformat(),
                        "--to",
                        t_to.date().isoformat(),
                    ]
                    log.info("watcher.wf.launch", strategy=strategy)
                    try:
                        proc = await asyncio.create_subprocess_exec(*argv)
                        await proc.wait()
                    except Exception as e:
                        log.warning(
                            "watcher.wf.err",
                            strategy=strategy,
                            err=str(e),
                        )
            await asyncio.sleep(60)

    async def run_control_observability(self) -> None:
        if not self.settings.observability_loop_enabled:
            log.info("watcher.control_observability.disabled")
            return
        log.info(
            "watcher.control_observability.started",
            interval_s=self.settings.observability_interval_s,
        )
        while True:
            try:
                await self._observe_once()
            except Exception as e:
                log.warning("watcher.control_observability.err", err=str(e))
            await asyncio.sleep(max(60, self.settings.observability_interval_s))

    async def _observe_once(self) -> None:
        status = await control_tools.get_system_status(self._control_api)
        engine_down = not bool(status.get("engine_up"))
        if engine_down and not self._engine_alert_active:
            await self.tg.send(
                T.AlertEvent(
                    kind="CONTROL_ENGINE_DOWN",
                    text="control loop detected engine down via /health",
                    severity=T.Severity.CRIT,
                )
            )
        self._engine_alert_active = engine_down

        kill_switch_on = bool(status.get("kill_switch_active"))
        if kill_switch_on and not self._killswitch_alert_active:
            await self.tg.send(
                T.AlertEvent(
                    kind="CONTROL_KILL_SWITCH_ON",
                    text="control loop detected KILL_SWITCH active",
                    severity=T.Severity.CRIT,
                )
            )
        self._killswitch_alert_active = kill_switch_on

        pnl = await control_tools.get_pnl(self._control_api, period="today")
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        if (
            float(pnl.get("pnl") or 0.0) <= self.settings.observability_pnl_alert_threshold
            and self._pnl_alert_day != today
        ):
            self._pnl_alert_day = today
            await self.tg.send(
                T.AlertEvent(
                    kind="CONTROL_PNL_ALERT",
                    text=(
                        f"daily pnl anomaly detected: ${float(pnl.get('pnl') or 0.0):,.2f} "
                        f"across {int(pnl.get('n_trades') or 0)} trades"
                    ),
                    severity=T.Severity.WARN,
                )
            )

        trades = await control_tools.get_recent_trades(self._control_api, limit=1)
        latest = (trades.get("trades") or [None])[0]
        stale = self._trade_is_stale(latest)
        if stale and not self._stale_trades_alert_active:
            await self.tg.send(
                T.AlertEvent(
                    kind="CONTROL_STALE_TRADES",
                    text=(
                        "recent trade feed looks stale based on /trades/recent "
                        f"(threshold {self.settings.observability_stale_trade_minutes} min)"
                    ),
                    severity=T.Severity.WARN,
                )
            )
        self._stale_trades_alert_active = stale

    def _trade_is_stale(self, trade: dict | None) -> bool:
        if not trade:
            return True
        ts_raw = trade.get("ts_submit") or trade.get("ts")
        if not ts_raw or not isinstance(ts_raw, str):
            return True
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        age_s = (datetime.now(tz=UTC) - ts).total_seconds()
        return age_s > self.settings.observability_stale_trade_minutes * 60


async def main_async() -> None:
    settings = get_settings()
    redis_url = f"redis://{settings.redis_host}:{settings.redis_port}/0"
    w = Watcher(redis_url)
    poller = CommandPoller()
    await asyncio.gather(
        w.run_heartbeat(),
        w.run_daily_report(),
        w.run_weekly_comparison(),
        w.run_walk_forward_sunday(),
        w.run_control_observability(),
        poller.run(),
    )


def main() -> None:
    configure_logging()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
