"""SimulatedExecutionClient for paper mode.

Reuses fill_model.simulate_fill (slippage, fill probability) and
fill_model.settle (win/loss resolution against the settle price).
Adds paper-specific gates:
  - KILL_SWITCH check before every order submit.
  - Stale-book guard: reject when the CLOB book snapshot is > STALE_BOOK_SECONDS old.
  - Late-entry guard: reject when t_in_window would land within LATE_ENTRY_GUARD of close.
  - Deterministic client_order_id: sha256(strategy | slug | ts | side)[:16].
  - Simulated latency: +LATENCY_MS between submit and fill timestamps.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading.common.db import upsert_many
from trading.common.logging import get_logger
from trading.engine.fill_model import FillParams, settle, simulate_fill
from trading.engine.types import Side

log = get_logger(__name__)

KILL_SWITCH_PATH = "/etc/trading-system/KILL_SWITCH"
STALE_BOOK_SECONDS = 10.0
LATE_ENTRY_GUARD = 5.0  # reject if t_in_window > latest - 5s
LATENCY_MS = 100


@dataclass
class Position:
    market_slug: str
    condition_id: str
    side: Side
    entry_ts: float
    entry_price: float
    stake_usd: float
    slippage: float
    fee: float
    client_order_id: str


def _client_order_id(strategy: str, slug: str, ts: float, side: Side) -> str:
    key = f"{strategy}|{slug}|{ts:.6f}|{side.value}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def _now_dt():
    return datetime.now(tz=UTC)


class SimulatedExecutionClient:
    mode = "paper"

    def __init__(self, strategy_id: str, fill_params: FillParams) -> None:
        self.strategy_id = strategy_id
        self.fill_params = fill_params

    @staticmethod
    def kill_switch_active() -> bool:
        return os.path.exists(KILL_SWITCH_PATH)

    async def try_enter(
        self,
        *,
        ts: float,
        condition_id: str,
        slug: str,
        side: Side,
        stake_usd: float,
        pm_yes_ask: float,
        pm_no_ask: float,
        book_last_update_ts: float,
        t_in_window: float,
        latest_entry_t: float,
    ) -> Position | None:
        if self.kill_switch_active():
            log.warning(
                "paper.exec.kill_switch_block",
                slug=slug,
                side=side.value,
            )
            return None
        if t_in_window > latest_entry_t - LATE_ENTRY_GUARD:
            log.info(
                "paper.exec.late_entry_reject",
                slug=slug,
                t_in_window=t_in_window,
            )
            return None
        if ts - book_last_update_ts > STALE_BOOK_SECONDS:
            log.warning(
                "paper.exec.stale_book_reject",
                slug=slug,
                age_s=ts - book_last_update_ts,
            )
            return None

        coid = _client_order_id(self.strategy_id, slug, ts, side)
        fill = simulate_fill(
            side=side,
            pm_yes_ask=pm_yes_ask,
            pm_no_ask=pm_no_ask,
            stake_usd=stake_usd,
            params=self.fill_params,
            seed_source=coid,
        )
        if not fill.filled:
            log.info("paper.exec.fill_miss", slug=slug, coid=coid)
            return None

        ts_submit = ts
        ts_fill = ts + LATENCY_MS / 1000.0

        # Persist order + fill with ON CONFLICT DO NOTHING (idempotent on restart).
        try:
            await upsert_many(
                "trading.orders",
                [
                    "order_id",
                    "strategy_id",
                    "instrument_id",
                    "side",
                    "order_type",
                    "qty",
                    "price",
                    "status",
                    "ts_submit",
                    "ts_last_update",
                    "mode",
                    "backtest_id",
                    "metadata",
                ],
                [
                    (
                        coid,
                        self.strategy_id,
                        f"{slug}-{'YES' if side is Side.YES_UP else 'NO'}.POLYMARKET",
                        "BUY",
                        "LIMIT",
                        Decimal(str(stake_usd / fill.entry_price)),
                        Decimal(str(fill.entry_price)),
                        "FILLED",
                        _dt(ts_submit),
                        _dt(ts_fill),
                        self.mode,
                        None,
                        "{}",
                    )
                ],
                ["order_id", "ts_submit"],
            )
            await upsert_many(
                "trading.fills",
                [
                    "fill_id",
                    "order_id",
                    "ts",
                    "price",
                    "qty",
                    "liquidity_side",
                    "fee",
                    "fee_currency",
                    "mode",
                    "backtest_id",
                    "metadata",
                ],
                [
                    (
                        f"{coid}-fill",
                        coid,
                        _dt(ts_fill),
                        Decimal(str(fill.entry_price)),
                        Decimal(str(stake_usd / fill.entry_price)),
                        "TAKER",
                        Decimal(str(fill.fee)),
                        "USDC",
                        self.mode,
                        None,
                        '{"kind":"entry"}',
                    )
                ],
                ["fill_id", "ts"],
            )
        except Exception as e:
            log.error("paper.exec.persist_err", err=str(e))
            return None

        pos = Position(
            market_slug=slug,
            condition_id=condition_id,
            side=side,
            entry_ts=ts_submit,
            entry_price=fill.entry_price,
            stake_usd=stake_usd,
            slippage=fill.slippage,
            fee=fill.fee,
            client_order_id=coid,
        )
        log.info(
            "paper.exec.entry",
            slug=slug,
            side=side.value,
            price=fill.entry_price,
            stake=stake_usd,
            coid=coid,
        )
        return pos

    async def settle(
        self, position: Position, *, settle_ts: float, settle_price: float, outcome_went_up: bool
    ) -> tuple[str, float, float]:
        resolution, exit_price, pnl = settle(
            side=position.side,
            entry_price=position.entry_price,
            stake_usd=position.stake_usd,
            fee=0.0,  # fee already captured on entry fill
            outcome_went_up=outcome_went_up,
        )
        exit_fill_id = f"{position.client_order_id}-exit"
        try:
            await upsert_many(
                "trading.fills",
                [
                    "fill_id",
                    "order_id",
                    "ts",
                    "price",
                    "qty",
                    "liquidity_side",
                    "fee",
                    "fee_currency",
                    "mode",
                    "backtest_id",
                    "metadata",
                ],
                [
                    (
                        exit_fill_id,
                        position.client_order_id,
                        _dt(settle_ts),
                        Decimal(str(exit_price)),
                        Decimal(str(position.stake_usd / max(position.entry_price, 1e-9))),
                        "TAKER",
                        Decimal("0"),
                        "USDC",
                        self.mode,
                        None,
                        f'{{"kind":"settle","resolution":"{resolution}","pnl":{pnl}}}',
                    )
                ],
                ["fill_id", "ts"],
            )
        except Exception as e:
            log.error("paper.exec.settle_persist_err", err=str(e))
        log.info(
            "paper.exec.settle",
            slug=position.market_slug,
            resolution=resolution,
            pnl=round(pnl, 2),
            coid=position.client_order_id,
        )
        return resolution, exit_price, pnl


def _dt(ts: float):
    return datetime.fromtimestamp(ts, tz=UTC)
