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

        # Pause state (ADR 0009). Controlled via Redis channel
        # tea:control:<strategy_name> + persisted in trading.strategy_state so
        # pauses survive engine restarts.
        self._paused: bool = False
        self._control_channel: str = f"tea:control:{self.strategy.name}"

        # Eval counters. Flushed on a 60s cadence so silent filtering is visible.
        self._eval_counts: dict[str, int] = {}
        self._eval_skip_reasons: dict[str, int] = {}

    def _bump_counter(self, bucket: str, reason: str | None = None) -> None:
        self._eval_counts[bucket] = self._eval_counts.get(bucket, 0) + 1
        if reason:
            self._eval_skip_reasons[reason] = self._eval_skip_reasons.get(reason, 0) + 1

    async def run(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=False)
        await self._rehydrate_pause_state()
        await self._rehydrate_trade_taken()
        self.strategy.on_start()
        log.info(
            "paper.driver.started",
            strategy=self.strategy.name,
            paused=self._paused,
        )
        reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        kill_switch_task = asyncio.create_task(self._kill_switch_loop())
        eval_summary_task = asyncio.create_task(self._eval_summary_loop())
        settle_watchdog_task = asyncio.create_task(self._settle_watchdog_loop())
        # Wrap the pubsub listen in a retry loop so a Redis hiccup
        # doesn't silently kill the driver. Exponential backoff, capped
        # at 30 s. Exit only on task cancellation.
        backoff = 1.0
        try:
            while True:
                try:
                    async with self._redis.pubsub() as pubsub:
                        await pubsub.subscribe(
                            REDIS_CHANNEL,
                            self._control_channel,
                        )
                        backoff = 1.0  # reset on successful subscribe
                        async for msg in pubsub.listen():
                            if msg is None or msg.get("type") != "message":
                                continue
                            channel = (
                                msg["channel"].decode()
                                if isinstance(msg["channel"], bytes | bytearray)
                                else msg["channel"]
                            )
                            if channel == self._control_channel:
                                self._handle_control_message(msg["data"])
                                continue
                            try:
                                tick_dict = json.loads(msg["data"])
                            except Exception:
                                continue
                            try:
                                await self._handle_tick(tick_dict)
                            except Exception as e:
                                log.exception(
                                    "paper.driver.handle_tick.err",
                                    err=str(e),
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error(
                        "paper.driver.pubsub.disconnected",
                        err=str(e),
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    log.info("paper.driver.pubsub.reconnecting")
        finally:
            reconciliation_task.cancel()
            kill_switch_task.cancel()
            eval_summary_task.cancel()
            settle_watchdog_task.cancel()
            self.strategy.on_stop()

    async def _rehydrate_trade_taken(self) -> None:
        """Rehydrate the ``_trade_taken`` set from recent paper orders.

        Prevents a double-fill on the same market after an engine
        restart mid-window: without this, the in-memory set is empty at
        boot and the strategy would re-ENTER any market that still has
        time left in its entry window.
        """
        try:
            async with acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT "
                    "  regexp_replace(instrument_id, '-(YES|NO)\\.POLYMARKET$', '') "
                    "    AS slug "
                    "FROM trading.orders "
                    "WHERE mode = 'paper' "
                    "  AND strategy_id = $1 "
                    "  AND ts_submit > now() - interval '10 minutes'",
                    self.strategy.name,
                )
        except Exception as e:
            log.warning("paper.driver.rehydrate_trade_taken_err", err=str(e))
            return
        for r in rows:
            slug = r["slug"]
            if slug:
                self._trade_taken.add(slug)
        if rows:
            log.info(
                "paper.driver.rehydrated_trade_taken",
                strategy=self.strategy.name,
                n_slugs=len(rows),
            )

    async def _rehydrate_pause_state(self) -> None:
        try:
            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state FROM trading.strategy_state WHERE strategy_id = $1",
                    self.strategy.name,
                )
        except Exception as e:
            log.warning("paper.driver.rehydrate_fail", err=str(e))
            return
        if row is None or row["state"] is None:
            return
        state = row["state"]
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except Exception:
                state = {}
        self._paused = bool(state.get("paused", False))
        if self._paused:
            log.warning(
                "paper.driver.rehydrated_paused",
                strategy=self.strategy.name,
                by=state.get("by"),
            )

    def _handle_control_message(self, raw) -> None:
        try:
            if isinstance(raw, bytes | bytearray):
                raw = raw.decode()
            payload = json.loads(raw)
        except Exception:
            log.warning("paper.driver.control.bad_payload", raw=str(raw)[:200])
            return
        action = payload.get("action")
        if action == "pause":
            self._paused = True
            log.warning(
                "paper.driver.paused",
                strategy=self.strategy.name,
                by=payload.get("by"),
            )
        elif action == "resume":
            self._paused = False
            log.info(
                "paper.driver.resumed",
                strategy=self.strategy.name,
                by=payload.get("by"),
            )
        else:
            log.warning("paper.driver.control.unknown_action", action=action)

    async def _eval_summary_loop(self) -> None:
        """Flush eval counters every 60 s. Makes silent filtering visible."""
        while True:
            await asyncio.sleep(60)
            if not self._eval_counts and not self._eval_skip_reasons:
                continue
            top_reasons = sorted(
                self._eval_skip_reasons.items(), key=lambda kv: kv[1], reverse=True
            )[:5]
            log.info(
                "paper.driver.eval_summary",
                ticks=self._eval_counts.get("ticks", 0),
                out_of_window=self._eval_counts.get("out_of_window", 0),
                in_entry_window=self._eval_counts.get("in_entry_window", 0),
                paused_skip=self._eval_counts.get("paused_skip", 0),
                risk_skip=self._eval_counts.get("risk_skip", 0),
                strategy_skip=self._eval_counts.get("strategy_skip", 0),
                enters=self._eval_counts.get("enter", 0),
                fills=self._eval_counts.get("fill", 0),
                fill_miss=self._eval_counts.get("fill_miss", 0),
                top_reasons=top_reasons,
            )
            self._eval_counts.clear()
            self._eval_skip_reasons.clear()

    async def _handle_tick(self, tick: dict) -> None:
        ctx = _tick_from_dict(tick)
        slug = ctx.market_slug
        # Rolling 120-tick buffer (~2 min at 1 Hz) + IndicatorStack per
        # market. The 30-tick window matched imbalance_v3's needs;
        # last_90s_forecaster_v1/_v2 need at least 90 samples to compute
        # the 90 s momentum, so we enlarge to 120 for headroom.
        indicators = self._indicators.setdefault(slug, IndicatorStack())
        buf = self._recent_ticks.setdefault(slug, [])
        ctx.recent_ticks = buf[-120:]
        buf.append(ctx)
        if len(buf) > 140:
            buf.pop(0)
        indicators.update(ctx)

        self._roll_day(ctx.ts)
        self._bump_counter("ticks")

        # Pause gate (ADR 0009). Bump counter + return BEFORE risk/strategy so
        # the pause is visible in the 60 s eval summary. Invariant I.1 preserved:
        # strategy is never invoked while paused.
        if self._paused:
            self._bump_counter("paused_skip", reason="paused")
            return

        # Update heartbeat counters.
        self.heartbeat.n_open_positions = len(self._open_positions)
        self.heartbeat.n_trades_today = self._daily_trades

        # Daily pause gate.
        if self._daily_pnl <= self.cfg.daily_pause_pnl_threshold:
            if slug not in self._open_positions and slug not in self._trade_taken:
                self._trade_taken.add(slug)

        # Settle or skip if post-window / already-traded.
        if slug in self._open_positions:
            if ctx.t_in_window >= 300:
                await self._settle_position(slug, ctx)
            return
        if slug in self._trade_taken:
            if ctx.t_in_window >= 300:
                self._trade_taken.discard(slug)
                self._cleanup_market(slug)
            return
        if not (self.cfg.earliest_entry_t <= ctx.t_in_window <= self.cfg.latest_entry_t):
            self._bump_counter("out_of_window")
            if ctx.t_in_window > 300:
                self._cleanup_market(slug)
            return
        if self._daily_pnl <= self.cfg.daily_pause_pnl_threshold:
            self._bump_counter("risk_skip", reason="daily_pause")
            return

        self._bump_counter("in_entry_window")

        # Risk gate, then strategy. Count + bucket reasons so 60 s summary
        # reveals whether skips concentrate on a specific filter.
        allowed, reason = self.risk.can_enter(ctx)
        if not allowed:
            tag = reason.split(" ")[0] if reason else "unknown"
            # Normalize common tags to a short bucket.
            if "cooldown" in reason:
                tag = "cooldown"
            elif "z_score" in reason:
                tag = "risk_z_score"
            elif "edge" in reason:
                tag = "risk_edge"
            elif "spread" in reason:
                tag = "risk_spread"
            elif "depth" in reason:
                tag = "risk_depth"
            elif "circuit_breaker" in reason or "daily_loss_limit" in reason:
                tag = "risk_circuit"
            elif "cool-off" in reason:
                tag = "risk_cool_off"
            self._bump_counter("risk_skip", reason=tag)
            return
        decision = self.strategy.should_enter(ctx)
        if decision.action is not Action.ENTER:
            short = decision.reason.split(" (")[0] if decision.reason else "strategy_skip"
            self._bump_counter("strategy_skip", reason=short)
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
            window_close_ts=ctx.window_close_ts,
            open_price=ctx.open_price,
        )
        if pos is None:
            self._bump_counter("fill_miss")
            return
        self._open_positions[slug] = pos
        self._trade_taken.add(slug)
        self._daily_trades += 1
        self._bump_counter("enter")
        self._bump_counter("fill")
        await self.tg.send(
            T.trade_open(
                slug=slug, side=pos.side.value, price=pos.entry_price, stake_usd=pos.stake_usd
            )
        )

    async def _settle_position(self, slug: str, ctx: TickContext) -> None:
        """Settle driven by an incoming tick past t_in_window=300.

        Delegates to the watchdog's spot-based math so both code paths
        (in-tick settle and out-of-band watchdog settle) agree on what
        "open" and "close" mean. ctx.open_price is NOT used here — it's
        the frozen value captured by the recorder at entry time and can
        drift from the true window-open spot after Chainlink updates.
        """
        pos = self._open_positions.get(slug)
        if pos is None:
            return
        import time as _time

        await self._settle_via_watchdog(slug, pos, now=_time.time())

    async def _settle_from_values(
        self,
        slug: str,
        *,
        settle_ts: float,
        settle_price: float,
        went_up: bool,
        source: str,
        fresh_open_price: float | None = None,
    ) -> None:
        pos = self._open_positions.pop(slug, None)
        if pos is None:
            return
        resolution, _exit_price, pnl = await self.exec.settle(
            pos,
            settle_ts=settle_ts,
            settle_price=settle_price,
            outcome_went_up=went_up,
        )
        self._daily_pnl += pnl
        self.risk.on_trade_closed(pnl, now=settle_ts)
        await self.tg.send(T.trade_close(resolution=resolution, pnl=pnl, slug=slug))
        log.info(
            "paper.driver.settled",
            slug=slug,
            source=source,
            settle_price=settle_price,
            open_price=fresh_open_price if fresh_open_price is not None else pos.open_price,
            pos_open_price=pos.open_price,
            went_up=went_up,
            pnl=pnl,
        )
        # Alert-only threshold.
        if self._daily_pnl <= self.cfg.daily_alert_pnl_threshold:
            await self.tg.send(
                T.loss_threshold(
                    self._daily_pnl,
                    pct=self._daily_pnl / max(self.cfg.stake_usd, 1.0) / 333.33,  # approx %
                )
            )
        self._cleanup_market(slug)

    async def _settle_watchdog_loop(self) -> None:
        """Periodically settle open positions whose window has already closed.

        The live recorder stops publishing ticks for markets that went
        ``closed=true`` on gamma, so ``_handle_tick`` rarely sees a tick
        with ``t_in_window >= 300``. This loop walks the in-memory
        positions and, for any whose ``window_close_ts`` is in the past
        by > 15 s, derives the settle price from the last recorded
        ``market_data.paper_ticks`` row and, failing that, from the 1 m
        Binance candle at ``window_close_ts``.
        """
        import time

        while True:
            try:
                await asyncio.sleep(15)
                now = time.time()
                expired = [
                    (slug, pos)
                    for slug, pos in list(self._open_positions.items())
                    if pos.window_close_ts > 0 and (now - pos.window_close_ts) > 15
                ]
                for slug, pos in expired:
                    await self._settle_via_watchdog(slug, pos, now)
            except Exception as e:
                log.warning("paper.driver.settle_watchdog.err", err=str(e))

    async def _settle_via_watchdog(self, slug: str, pos: Position, now: float) -> None:
        age = now - pos.window_close_ts
        # Step 1: last paper_tick for this market.
        settle_price, settle_ts, source = await self._last_paper_tick_price(
            pos.condition_id, pos.window_close_ts
        )
        if settle_price is None and age > 120:
            # Step 2: Binance 1 m close for the minute of window_close_ts.
            settle_price, settle_ts = await self._ohlcv_close_at(pos.window_close_ts)
            source = "ohlcv"
        if settle_price is None:
            if age > 600:  # 10 min past close and still nothing → give up.
                log.error(
                    "paper.driver.settle_timeout",
                    slug=slug,
                    close_ts=pos.window_close_ts,
                )
                # Drop so we don't keep retrying forever; operator can
                # backfill if needed.
                self._open_positions.pop(slug, None)
                self._cleanup_market(slug)
            return
        # pos.open_price is captured at ENTRY time (typically t=180–215),
        # by which point the Chainlink feed can already be off-window for
        # this market. The truthful open-vs-close comparison is the
        # FIRST paper_tick for this market (t_in_window ≈ 0) vs the
        # LAST paper_tick (t_in_window ≈ 300). Fetch both spots and use
        # those; fall back to ohlcv 1m candle if either is missing.
        open_price, open_source = await self._first_paper_tick_spot(
            pos.condition_id,
            window_close_ts=pos.window_close_ts,
        )
        if open_price is None:
            open_price, _ = await self._ohlcv_close_at(pos.window_close_ts - 300)
            open_source = "ohlcv"
        if open_price is None or open_price <= 0:
            log.error(
                "paper.driver.settle_no_open_price",
                slug=slug,
                close_ts=pos.window_close_ts,
            )
            return
        source = f"{source}+open={open_source}"
        went_up = settle_price > open_price
        await self._settle_from_values(
            slug,
            settle_ts=settle_ts,
            settle_price=settle_price,
            went_up=went_up,
            source=source,
            fresh_open_price=open_price,
        )

    async def _last_paper_tick_price(
        self, condition_id: str, close_ts: float
    ) -> tuple[float | None, float, str]:
        """Return the most recent ``spot_price`` from
        ``market_data.paper_ticks`` for this market, published at or
        before ``close_ts + 5 s``.

        We intentionally use spot rather than chainlink_price here:
        Polymarket resolves against Chainlink Data Streams but our
        paper-tick recorder reads the on-chain EAC feed via Alchemy
        RPC, which updates very infrequently (~once per window) and
        can freeze entirely when the RPC hiccups. Spot (Binance 1 Hz
        WebSocket) is the most reliable directional signal we have in
        paper; live + contest evaluation will use a real Chainlink
        Data Streams feed when the key is provisioned (ADR 0010
        addendum).
        """
        # Time-bound to the current window. The recorder sometimes writes
        # ticks for a market before its window opens (discovery
        # lookahead) and the same condition_id may have stale ticks from
        # an earlier day; without the lower bound the "latest" row could
        # be hours old and flip went_up by hundreds of bps.
        window_start_dt = datetime.fromtimestamp(close_ts - 305, tz=UTC)
        cutoff_dt = datetime.fromtimestamp(close_ts + 5, tz=UTC)
        try:
            async with acquire() as conn:
                latest = await conn.fetchrow(
                    "SELECT ts, spot_price, chainlink_price "
                    "FROM market_data.paper_ticks "
                    "WHERE condition_id = $1 "
                    "  AND ts BETWEEN $2 AND $3 "
                    "ORDER BY ts DESC LIMIT 1",
                    condition_id,
                    window_start_dt,
                    cutoff_dt,
                )
        except Exception as e:
            log.warning("paper.driver.settle.paper_tick_query_err", err=str(e))
            return None, close_ts, "paper_tick_err"
        if latest is None:
            return None, close_ts, "no_paper_tick"
        sp_now = latest["spot_price"]
        if sp_now is not None:
            return float(sp_now), latest["ts"].timestamp(), "paper_tick_spot"
        cl_now = latest["chainlink_price"]
        if cl_now is not None:
            return float(cl_now), latest["ts"].timestamp(), "paper_tick_chainlink"
        return None, close_ts, "no_price_in_tick"

    async def _first_paper_tick_spot(
        self,
        condition_id: str,
        window_close_ts: float,
    ) -> tuple[float | None, str]:
        """Return the spot_price of the first paper_tick for THIS
        window (≈ window_close_ts - 300).

        ``window_close_ts`` is required. The recorder sometimes writes
        ticks for a market hours before its window opens (discovery
        lookahead) or under backfills with a reset t_in_window; without
        the time bound the "earliest" row can be from a completely
        different window and computes went_up backwards.
        """
        if window_close_ts is None or window_close_ts <= 0:
            raise ValueError("window_close_ts is required to prevent stale cross-window picks")
        window_start_dt = datetime.fromtimestamp(window_close_ts - 305, tz=UTC)
        window_end_dt = datetime.fromtimestamp(window_close_ts + 5, tz=UTC)
        try:
            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT spot_price "
                    "FROM market_data.paper_ticks "
                    "WHERE condition_id = $1 "
                    "  AND ts BETWEEN $2 AND $3 "
                    "  AND spot_price IS NOT NULL AND spot_price > 0 "
                    "ORDER BY ts ASC LIMIT 1",
                    condition_id,
                    window_start_dt,
                    window_end_dt,
                )
        except Exception as e:
            log.warning("paper.driver.settle.first_tick_err", err=str(e))
            return None, "first_tick_err"
        if row is None or row["spot_price"] is None:
            return None, "no_first_tick"
        return float(row["spot_price"]), "paper_tick_first_spot"

    async def _ohlcv_close_at(self, close_ts: float) -> tuple[float | None, float]:
        """Look up the Binance 1 m candle whose ``ts`` equals the minute of
        ``close_ts`` for BTCUSDT and return its close price."""
        minute_ts = int(close_ts // 60 * 60)
        minute_dt = datetime.fromtimestamp(minute_ts, tz=UTC)
        try:
            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT close FROM market_data.crypto_ohlcv "
                    "WHERE exchange='binance' AND symbol='BTCUSDT' "
                    "AND interval='1m' AND ts=$1",
                    minute_dt,
                )
        except Exception as e:
            log.warning("paper.driver.settle.ohlcv_err", err=str(e))
            return None, close_ts
        if row is None:
            return None, close_ts
        return float(row["close"]), close_ts

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
        """Watch the kill switch files (dual path per ADR 0009) and emit
        transition alerts."""
        from trading.engine.node import KILL_SWITCH_PATHS

        while True:
            present = any(os.path.exists(p) for p in KILL_SWITCH_PATHS)
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
    spot = float(d.get("spot_price", 0.0) or 0.0)
    open_price = float(d.get("open_price", 0.0) or 0.0)
    delta_bps = 0.0
    if open_price > 0:
        delta_bps = (spot - open_price) / open_price * 10000.0
    return TickContext(
        ts=ts,
        market_slug=d["market_slug"],
        t_in_window=float(d.get("t_in_window", 0.0)),
        window_close_ts=close_ts,
        spot_price=spot,
        chainlink_price=float(d.get("chainlink_price", 0.0) or 0.0) or None,
        open_price=open_price,
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
        delta_bps=delta_bps,
    )


def _condition_id_placeholder(tick: dict, slug: str) -> str:
    # The recorder includes condition_id in the payload, but if older
    # versions of the pipeline omitted it we can synthesize one from the slug.
    return tick.get("condition_id") or f"local:{slug}"
