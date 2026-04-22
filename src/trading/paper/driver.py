"""Paper trading driver: consumes tick stream from Redis, calls the
Strategy + RiskManager + SimulatedExecutionClient, settles on window
close. Mirrors the Phase 2 backtest_driver shape so the Strategy code
path is identical across modes (Invariant I.1)."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as redis

from trading.common.db import acquire
from trading.common.logging import get_logger
from trading.engine.indicators import IndicatorStack
from trading.engine.risk import RiskManager
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, TickContext
from trading.notifications import telegram as T
from trading.paper.exec_client import Position, SimulatedExecutionClient
from trading.paper.heartbeat import HeartbeatPublisher
from trading.paper.tick_recorder import REDIS_CHANNEL

log = get_logger(__name__)


@dataclass
class DriverConfig:
    strategy_id: str
    stake_usd: float
    earliest_entry_t: int
    latest_entry_t: int
    daily_alert_pnl_threshold: float  # e.g. -30.0 for -3% of $1000
    daily_pause_pnl_threshold: float  # e.g. -50.0
    reconciliation_interval_s: int = 300


class PaperDriver:
    def __init__(
        self,
        strategy: StrategyBase,
        risk_manager: RiskManager,
        exec_client: SimulatedExecutionClient,
        tg: T.TelegramClient,
        heartbeat: HeartbeatPublisher,
        cfg: DriverConfig,
        redis_url: str,
    ) -> None:
        self.strategy = strategy
        self.risk = risk_manager
        self.exec = exec_client
        self.tg = tg
        self.heartbeat = heartbeat
        self.cfg = cfg
        self.redis_url = redis_url
        self._redis: redis.Redis | None = None

        # Per-market state.
        self._indicators: dict[str, IndicatorStack] = {}
        self._recent_ticks: dict[str, list[TickContext]] = {}
        self._open_positions: dict[str, Position] = {}
        self._trade_taken: set[str] = set()  # slugs where we already fired ENTER

        # Daily stats.
        self._today: str = ""
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

        # Kill switch state for edge-triggered alerts.
        self._kill_switch_last_state: bool = False

    async def run(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=False)
        self.strategy.on_start()
        log.info("paper.driver.started", strategy=self.strategy.name)
        reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        kill_switch_task = asyncio.create_task(self._kill_switch_loop())
        try:
            async with self._redis.pubsub() as pubsub:
                await pubsub.subscribe(REDIS_CHANNEL)
                async for msg in pubsub.listen():
                    if msg is None or msg.get("type") != "message":
                        continue
                    try:
                        tick_dict = json.loads(msg["data"])
                    except Exception:
                        continue
                    await self._handle_tick(tick_dict)
        finally:
            reconciliation_task.cancel()
            kill_switch_task.cancel()
            self.strategy.on_stop()

    async def _handle_tick(self, tick: dict) -> None:
        ctx = _tick_from_dict(tick)
        slug = ctx.market_slug
        # Rolling 30-tick buffer + IndicatorStack per market.
        indicators = self._indicators.setdefault(slug, IndicatorStack())
        buf = self._recent_ticks.setdefault(slug, [])
        ctx.recent_ticks = buf[-30:]
        buf.append(ctx)
        if len(buf) > 60:
            buf.pop(0)
        indicators.update(ctx)

        self._roll_day(ctx.ts)

        # Update heartbeat counters.
        self.heartbeat.n_open_positions = len(self._open_positions)
        self.heartbeat.n_trades_today = self._daily_trades

        # Daily pause gate.
        if self._daily_pnl <= self.cfg.daily_pause_pnl_threshold:
            # Threshold in USD (e.g. -50.0); already hit → no new entries today.
            if slug not in self._open_positions and slug not in self._trade_taken:
                self._trade_taken.add(slug)  # won't try this market again today
            # Still need to settle open positions if any.
            pass

        # Try enter if no open position on this market and entry window open.
        if slug in self._open_positions:
            if ctx.t_in_window >= 300:
                await self._settle_position(slug, ctx)
            return
        if slug in self._trade_taken:
            if ctx.t_in_window >= 300:
                # Past close but no position, nothing to do.
                self._trade_taken.discard(slug)
                self._cleanup_market(slug)
            return
        if not (self.cfg.earliest_entry_t <= ctx.t_in_window <= self.cfg.latest_entry_t):
            if ctx.t_in_window > 300:
                self._cleanup_market(slug)
            return
        if self._daily_pnl <= self.cfg.daily_pause_pnl_threshold:
            return

        # Risk gate, then strategy.
        allowed, reason = self.risk.can_enter(ctx)
        if not allowed:
            return
        decision = self.strategy.should_enter(ctx)
        if decision.action is not Action.ENTER:
            return

        book_last_ts = ctx.ts  # feeds keep this updated via tick recorder; approximate
        pos = await self.exec.try_enter(
            ts=ctx.ts,
            condition_id=_condition_id_placeholder(tick, slug),
            slug=slug,
            side=decision.side,
            stake_usd=self.cfg.stake_usd,
            pm_yes_ask=ctx.pm_yes_ask,
            pm_no_ask=ctx.pm_no_ask,
            book_last_update_ts=book_last_ts,
            t_in_window=ctx.t_in_window,
            latest_entry_t=float(self.cfg.latest_entry_t),
        )
        if pos is None:
            return
        self._open_positions[slug] = pos
        self._trade_taken.add(slug)
        self._daily_trades += 1
        await self.tg.send(
            T.trade_open(
                slug=slug, side=pos.side.value, price=pos.entry_price, stake_usd=pos.stake_usd
            )
        )

    async def _settle_position(self, slug: str, ctx: TickContext) -> None:
        pos = self._open_positions.pop(slug, None)
        if pos is None:
            return
        settle_price = ctx.chainlink_price or ctx.spot_price
        if settle_price <= 0:
            log.error("paper.driver.settle_no_price", slug=slug)
            return
        # open_price already captured in tick recorder → ctx.open_price.
        went_up = settle_price > ctx.open_price
        resolution, _exit_price, pnl = await self.exec.settle(
            pos,
            settle_ts=ctx.ts,
            settle_price=settle_price,
            outcome_went_up=went_up,
        )
        self._daily_pnl += pnl
        self.risk.on_trade_closed(pnl, now=ctx.ts)
        await self.tg.send(T.trade_close(resolution=resolution, pnl=pnl, slug=slug))

        # Alert-only threshold.
        if self._daily_pnl <= self.cfg.daily_alert_pnl_threshold:
            await self.tg.send(
                T.loss_threshold(
                    self._daily_pnl,
                    pct=self._daily_pnl / max(self.cfg.stake_usd, 1.0) / 333.33,  # approx %
                )
            )

        self._cleanup_market(slug)

    def _cleanup_market(self, slug: str) -> None:
        self._indicators.pop(slug, None)
        self._recent_ticks.pop(slug, None)

    def _roll_day(self, ts: float) -> None:
        day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
        if day != self._today:
            self._today = day
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._trade_taken.clear()

    async def _kill_switch_loop(self) -> None:
        """Watch the kill switch file and emit transition alerts."""
        while True:
            present = os.path.exists("/etc/trading-system/KILL_SWITCH")
            if present and not self._kill_switch_last_state:
                await self.tg.send(T.kill_switch_on(datetime.now(tz=UTC).isoformat()))
            elif not present and self._kill_switch_last_state:
                await self.tg.send(T.kill_switch_off(datetime.now(tz=UTC).isoformat()))
            self._kill_switch_last_state = present
            await asyncio.sleep(5)

    async def _reconciliation_loop(self) -> None:
        """Every N seconds compare in-memory ledger vs trading.fills DB state.

        ALERT-only (not auto-pause, per Phase 3 design adjustment): divergence
        triggers a CRIT alert + log, operator decides next step via kill
        switch or /pause (Phase 4).
        """
        while True:
            await asyncio.sleep(self.cfg.reconciliation_interval_s)
            try:
                async with acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT COALESCE(SUM(
                            CASE WHEN metadata::jsonb->>'kind' = 'settle'
                                 THEN ((metadata::jsonb->>'pnl')::numeric)
                                 ELSE 0 END), 0) AS settled_pnl,
                               COUNT(*) FILTER (
                                   WHERE metadata::jsonb->>'kind' = 'entry'
                                 ) AS entries_today,
                               COUNT(*) FILTER (
                                   WHERE metadata::jsonb->>'kind' = 'settle'
                                 ) AS exits_today
                        FROM trading.fills
                        WHERE mode='paper' AND ts >= (now() - interval '1 day')
                        """
                    )
                db_settled = float(row["settled_pnl"] or 0.0)
                db_entries = int(row["entries_today"] or 0)
                db_exits = int(row["exits_today"] or 0)
                local_closed = self._daily_pnl
                # Skip zero-state comparison.
                if db_entries == 0 and self._daily_trades == 0:
                    continue
                delta = abs(db_settled - local_closed)
                open_diff = (db_entries - db_exits) - len(self._open_positions)
                if delta > 1.0 or abs(open_diff) > 1:
                    detail = (
                        f"db_settled=${db_settled:.2f} local=${local_closed:.2f} "
                        f"delta=${delta:.2f} open_db={db_entries - db_exits} "
                        f"open_local={len(self._open_positions)}"
                    )
                    log.error("paper.reconciliation.fail", detail=detail)
                    await self.tg.send(T.reconciliation_fail(detail))
                else:
                    log.info(
                        "paper.reconciliation.ok",
                        db_settled=db_settled,
                        local=local_closed,
                        open=len(self._open_positions),
                    )
            except Exception as e:
                log.warning("paper.reconciliation.err", err=str(e))


def _tick_from_dict(d: dict) -> TickContext:
    ts = float(d["ts"])
    close_ts = float(d["window_close_ts"])
    return TickContext(
        ts=ts,
        market_slug=d["market_slug"],
        t_in_window=float(d.get("t_in_window", 0.0)),
        window_close_ts=close_ts,
        spot_price=float(d.get("spot_price", 0.0) or 0.0),
        chainlink_price=float(d.get("chainlink_price", 0.0) or 0.0) or None,
        open_price=float(d.get("open_price", 0.0) or 0.0),
        pm_yes_bid=float(d.get("pm_yes_bid", 0.0) or 0.0),
        pm_yes_ask=float(d.get("pm_yes_ask", 0.0) or 0.0),
        pm_no_bid=float(d.get("pm_no_bid", 0.0) or 0.0),
        pm_no_ask=float(d.get("pm_no_ask", 0.0) or 0.0),
        pm_depth_yes=float(d.get("pm_depth_yes", 0.0) or 0.0),
        pm_depth_no=float(d.get("pm_depth_no", 0.0) or 0.0),
        pm_imbalance=float(d.get("pm_imbalance", 0.0) or 0.0),
        pm_spread_bps=float(d.get("pm_spread_bps", 0.0) or 0.0),
        implied_prob_yes=float(d.get("implied_prob_yes", 0.0) or 0.0),
        model_prob_yes=0.0,
        edge=0.0,
        z_score=0.0,
        vol_regime="unknown",
        recent_ticks=[],
        t_to_close=max(0.0, close_ts - ts),
    )


def _condition_id_placeholder(tick: dict, slug: str) -> str:
    # The recorder includes condition_id in the payload, but if older
    # versions of the pipeline omitted it we can synthesize one from the slug.
    return tick.get("condition_id") or f"local:{slug}"
