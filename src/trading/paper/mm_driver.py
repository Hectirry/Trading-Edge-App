"""MM paper driver — sibling of PaperDriver for `on_tick`-style strategies.

Inherits from PaperDriver so per-market frame setup (rolling buffer,
heartbeat, paused gate) is reused. Overrides only the handler tail to
replace the one-shot ENTER flow with a continuous quoting flow:

  on_tick(ctx)            → list[MMAction]
  PostQuote               → limit_book_sim.place(LimitOrder)
  CancelQuote             → limit_book_sim.cancel(coid)
  ReplaceQuote            → cancel + place
  limit_book_sim.on_tick  → returns fills when book moves cross orders
  on_fill(...)            → strategy callback to update inventory

Existing direction-style strategies (last_90s_v3, trend_confirm_t1_v1,
oracle_lag_v1) continue to use PaperDriver — they are NOT touched by
this module.

Aggressive paper soak considerations (mm_rebate_v1 V1)
-------------------------------------------------------
- daily_pause_pnl_threshold inherited from DriverConfig; mm_rebate_v1's
  config sets it to a permissive value so the strategy does not auto-pause
  during the soak. Operator wants the full drawdown signal.
- Trade settlement on t_in_window=300 (window close) is owed but currently
  the limit_book_sim auto-cancels TTL'd orders; resolution PnL closes
  through the YES/NO terminal price (1.0 / 0.0) by the existing
  `_settle_position` path won't reach here because we never store a
  Position. Instead the inventory_pnl is realized by the next replace
  cycle's mid-price bookkeeping. Step 0 v2 / Step 3 will validate this.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.common.logging import get_logger
from trading.engine.mm_actions import CancelQuote, MMAction, PostQuote, ReplaceQuote
from trading.engine.types import Side
from trading.paper.driver import PaperDriver, _tick_from_dict
from trading.paper.limit_book_sim import LimitBookSim, LimitOrder

log = get_logger(__name__)


def _instrument_id_yes(slug: str) -> str:
    """Match exec_client's convention so trading.orders / fills stay joinable."""
    return f"{slug}-YES.POLYMARKET"


def _side_to_book_side(side: Side) -> str:
    """YES_UP = bid (BUY YES); YES_DOWN = ask (SELL YES)."""
    return "BUY" if side is Side.YES_UP else "SELL"


class MMPaperDriver(PaperDriver):
    def __init__(self, *args, limit_book: LimitBookSim, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.limit_book = limit_book

    async def _handle_tick(self, tick: dict) -> None:
        # Reuse parent setup for buffer + heartbeat + pause gate, then
        # branch off before parent's entry-window / one-shot-settle gates.
        ctx = _tick_from_dict(tick)
        slug = ctx.market_slug
        from trading.engine.indicators import IndicatorStack

        indicators = self._indicators.setdefault(slug, IndicatorStack())
        buf = self._recent_ticks.setdefault(slug, [])
        ctx.recent_ticks = buf[-120:]
        buf.append(ctx)
        if len(buf) > 140:
            buf.pop(0)
        self._last_tick_at[slug] = ctx.ts
        indicators.update(ctx)
        self._roll_day(ctx.ts)
        self._bump_counter("ticks")

        if self._paused:
            self._bump_counter("paused_skip", reason="paused")
            return

        self.heartbeat.n_open_positions = sum(
            1 for v in self.strategy._live_quotes_by_market.values() if v  # type: ignore[attr-defined]
        )
        self.heartbeat.n_trades_today = self._daily_trades

        # MM flow: continuous quoting. NO trade_taken / no entry_window
        # gating — the strategy decides per-tick whether it has anything
        # to do. Cleanup on post-window is implicit (strategy returns
        # cancels when t_in_window > T - tau_terminal).
        actions: list[MMAction] = self.strategy.on_tick(ctx)
        for action in actions:
            await self._dispatch_action(action, ctx, slug)

        # Run the limit-book matcher against the new YES mid. The mid is
        # `implied_prob_yes` (the strategy's view of p_fair) — book fills
        # land when the mid crosses a resting order.
        instrument_id = _instrument_id_yes(slug)
        mid = float(ctx.implied_prob_yes)
        if 0.0 < mid < 1.0:
            fills = await self.limit_book.on_tick(
                instrument_id=instrument_id, px=mid, ts=ctx.ts
            )
            for f in fills:
                # YES BUY at price p ⇒ strategy bought YES → inventory +q
                # YES SELL at price p ⇒ strategy sold YES → inventory −q
                strategy_side = Side.YES_UP if f.side == "BUY" else Side.YES_DOWN
                self.strategy.on_fill(
                    market_slug=slug,
                    client_order_id=f.coid,
                    side=strategy_side.value,
                    fill_price=f.price,
                    fill_qty_shares=f.qty,
                    ts=f.ts,
                )
                self._daily_trades += 1
                self._bump_counter("fill")
                # Telegram notification per fill (rate-limited at the
                # TelegramClient layer).
                from trading.notifications import telegram as T
                await self.tg.send(
                    T.trade_open(
                        slug=slug,
                        side=strategy_side.value,
                        price=f.price,
                        stake_usd=f.qty * f.price,
                    )
                )

    async def _dispatch_action(
        self, action: MMAction, ctx, slug: str
    ) -> None:
        instrument_id = _instrument_id_yes(slug)
        if isinstance(action, PostQuote):
            order = LimitOrder(
                coid=self._coid_for_post(slug, ctx.ts, action),
                strategy_id=self.strategy.name,
                instrument_id=instrument_id,
                side=_side_to_book_side(action.side),
                price=float(action.price),
                qty=float(action.qty_shares),
                ts_placed=float(ctx.ts),
                ttl_s=float(action.ttl_seconds) if action.ttl_seconds > 0 else None,
                metadata={"strategy": self.strategy.name, "seed": action.client_id_seed},
            )
            await self.limit_book.place(order)
            self._bump_counter("mm_post_quote")
        elif isinstance(action, CancelQuote):
            await self.limit_book.cancel(action.client_order_id, reason=action.reason or "user")
            self._bump_counter("mm_cancel_quote")
        elif isinstance(action, ReplaceQuote):
            ok = await self.limit_book.cancel(
                action.old_client_order_id, reason="replace"
            )
            if ok:
                new = action.new
                order = LimitOrder(
                    coid=self._coid_for_post(slug, ctx.ts, new),
                    strategy_id=self.strategy.name,
                    instrument_id=instrument_id,
                    side=_side_to_book_side(new.side),
                    price=float(new.price),
                    qty=float(new.qty_shares),
                    ts_placed=float(ctx.ts),
                    ttl_s=float(new.ttl_seconds) if new.ttl_seconds > 0 else None,
                    metadata={"strategy": self.strategy.name, "seed": new.client_id_seed},
                )
                await self.limit_book.place(order)
            self._bump_counter("mm_replace_quote")
        else:
            log.warning("mm_driver.unknown_action", action_type=type(action).__name__)

    def _coid_for_post(self, slug: str, ts: float, q: PostQuote) -> str:
        """Driver-side stable coid that matches the strategy's local ledger.
        Strategy uses the same hash inputs in _coid(); both must match.
        """
        import hashlib

        seed = q.client_id_seed
        side_val = q.side.value
        key = f"{self.strategy.name}|{slug}|{ts:.6f}|{side_val}|{seed}".encode()
        return hashlib.sha256(key).hexdigest()[:16]
