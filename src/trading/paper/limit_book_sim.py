"""In-memory limit order book simulator (ADR 3.8a).

Grid-trading strategies place N limit orders simultaneously and rely on
maker fills as spot crosses each level. ``SimulatedExecutionClient``
(exec_client.py) is decision-at-tick market-order-only and cannot model
this; ``LimitBookSim`` is its complement for continuous strategies.

Fill model
----------
BUY  limit at L fills when tick_px <= L.
SELL limit at L fills when tick_px >= L.
Fill price is L exactly (maker). Slippage = 0. Maker fee applied as
``(fee_bps / 10_000) * L * qty`` in quote currency.

Persistence
-----------
``place`` writes trading.orders with status=NEW (ts_submit).
``on_tick`` on fill writes trading.fills and updates trading.orders
status=FILLED + ts_last_update.
``cancel`` / ``cancel_all`` update trading.orders status=CANCELLED +
ts_last_update.

TTL expiry is checked on every ``on_tick`` and turns into a CANCELLED
order with metadata ``{"reason":"ttl"}`` — not a fill.

Determinism
-----------
Order of fills within a single tick: FIFO by ``ts_placed``. Fee model is
a flat bps number (provisional Binance spot 0.1% = 10 bps, to be replaced
by the tiered model in phase 3.8c).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.common.db import acquire, upsert_many
from trading.common.logging import get_logger

log = get_logger(__name__)

DEFAULT_MAKER_FEE_BPS = 10.0  # [PROVISIONAL] Binance spot 0.1% flat


@dataclass
class LimitOrder:
    coid: str
    strategy_id: str
    instrument_id: str
    side: str  # "BUY" or "SELL"
    price: float
    qty: float
    ts_placed: float
    ttl_s: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def expired_at(self, now: float) -> bool:
        return self.ttl_s is not None and now >= self.ts_placed + self.ttl_s


@dataclass
class LimitFill:
    coid: str
    strategy_id: str
    instrument_id: str
    side: str
    price: float
    qty: float
    ts: float
    fee: float
    liquidity_side: str = "MAKER"


def deterministic_coid(
    *,
    strategy_id: str,
    instrument_id: str,
    reset_gen: int,
    level_idx: int,
    side: str,
    center_price: float,
) -> str:
    """Stable SHA-256 coid that survives reset. Different reset_gen or
    center_price → different coid, so prior orders can't shadow-fill
    post-reset when duplicates somehow re-enter the book.
    """
    key = (
        f"{strategy_id}|{instrument_id}|{reset_gen}|{level_idx}|{side}|{center_price:.8f}"
    ).encode()
    return hashlib.sha256(key).hexdigest()[:16]


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


class LimitBookSim:
    """In-memory limit order book simulator, async-safe."""

    def __init__(
        self,
        *,
        mode: str = "paper",
        maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
        persist: bool = True,
        backtest_id: str | None = None,
    ) -> None:
        self._orders: dict[str, LimitOrder] = {}
        self._mode = mode
        self._maker_fee_bps = maker_fee_bps
        self._persist = persist
        self._backtest_id = backtest_id
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._orders)

    def snapshot(self) -> list[LimitOrder]:
        return list(self._orders.values())

    def get(self, coid: str) -> LimitOrder | None:
        return self._orders.get(coid)

    async def place(self, order: LimitOrder) -> bool:
        if order.side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side: {order.side}")
        if order.price <= 0 or order.qty <= 0:
            raise ValueError("price and qty must be > 0")
        async with self._lock:
            if order.coid in self._orders:
                log.warning(
                    "limit_book.place.duplicate_coid",
                    coid=order.coid,
                    strategy_id=order.strategy_id,
                )
                return False
            self._orders[order.coid] = order
        if self._persist:
            await self._persist_new(order)
        log.debug(
            "limit_book.place",
            coid=order.coid,
            side=order.side,
            price=order.price,
            qty=order.qty,
        )
        return True

    async def cancel(self, coid: str, *, reason: str = "user") -> bool:
        async with self._lock:
            order = self._orders.pop(coid, None)
        if order is None:
            return False
        if self._persist:
            await self._persist_cancel(order, reason=reason)
        log.debug("limit_book.cancel", coid=coid, reason=reason)
        return True

    async def cancel_all(
        self,
        *,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        side: str | None = None,
        reason: str = "reset",
    ) -> int:
        """Cancel every resting order matching the given filters.

        ``side`` (new 3.8a.2): when set to "BUY" or "SELL", only cancels
        orders on that side. Used by the continuous driver on Reset to
        preserve paired SELLs across grid resets — they are the closing
        orders for accumulated long inventory and must not be discarded.
        """
        async with self._lock:
            victims = [
                o
                for o in self._orders.values()
                if (strategy_id is None or o.strategy_id == strategy_id)
                and (instrument_id is None or o.instrument_id == instrument_id)
                and (side is None or o.side == side)
            ]
            for o in victims:
                self._orders.pop(o.coid, None)
        if victims and self._persist:
            for o in victims:
                await self._persist_cancel(o, reason=reason)
        log.info(
            "limit_book.cancel_all",
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            side=side,
            n=len(victims),
            reason=reason,
        )
        return len(victims)

    async def on_tick(
        self,
        *,
        instrument_id: str,
        px: float,
        ts: float,
    ) -> list[LimitFill]:
        """Evict TTL-expired orders, fill crossed limits, return fills."""
        fills: list[LimitFill] = []
        expired: list[LimitOrder] = []

        async with self._lock:
            # Sort FIFO by ts_placed for deterministic fill order.
            ordered = sorted(
                (o for o in self._orders.values() if o.instrument_id == instrument_id),
                key=lambda o: o.ts_placed,
            )
            for o in ordered:
                if o.expired_at(ts):
                    expired.append(o)
                    continue
                crossed = (o.side == "BUY" and px <= o.price) or (
                    o.side == "SELL" and px >= o.price
                )
                if not crossed:
                    continue
                fee = self._maker_fee_bps * 1e-4 * o.price * o.qty
                fills.append(
                    LimitFill(
                        coid=o.coid,
                        strategy_id=o.strategy_id,
                        instrument_id=o.instrument_id,
                        side=o.side,
                        price=o.price,
                        qty=o.qty,
                        ts=ts,
                        fee=fee,
                    )
                )
            for f in fills:
                self._orders.pop(f.coid, None)
            for e in expired:
                self._orders.pop(e.coid, None)

        if self._persist:
            for o in expired:
                await self._persist_cancel(o, reason="ttl")
            if fills:
                await self._persist_fills(fills)
        if fills or expired:
            log.debug(
                "limit_book.on_tick",
                instrument_id=instrument_id,
                px=px,
                fills=len(fills),
                expired=len(expired),
            )
        return fills

    async def _persist_new(self, order: LimitOrder) -> None:
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
                        order.coid,
                        order.strategy_id,
                        order.instrument_id,
                        order.side,
                        "LIMIT",
                        Decimal(str(order.qty)),
                        Decimal(str(order.price)),
                        "NEW",
                        _dt(order.ts_placed),
                        _dt(order.ts_placed),
                        self._mode,
                        self._backtest_id,
                        _json(order.metadata),
                    )
                ],
                ["order_id", "ts_submit"],
            )
        except Exception as e:
            log.error("limit_book.persist_new_err", coid=order.coid, err=str(e))

    async def _persist_cancel(self, order: LimitOrder, *, reason: str) -> None:
        try:
            async with acquire() as conn:
                await conn.execute(
                    "UPDATE trading.orders "
                    "SET status = $1, ts_last_update = $2, "
                    "metadata = coalesce(metadata, '{}'::jsonb) || $3::jsonb "
                    "WHERE order_id = $4 AND ts_submit = $5",
                    "CANCELLED",
                    _dt(order.ts_placed) if reason == "ttl" else datetime.now(tz=UTC),
                    f'{{"cancel_reason":"{reason}"}}',
                    order.coid,
                    _dt(order.ts_placed),
                )
        except Exception as e:
            log.error("limit_book.persist_cancel_err", coid=order.coid, err=str(e))

    async def _persist_fills(self, fills: list[LimitFill]) -> None:
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
                        f"{f.coid}-fill",
                        f.coid,
                        _dt(f.ts),
                        Decimal(str(f.price)),
                        Decimal(str(f.qty)),
                        f.liquidity_side,
                        Decimal(str(f.fee)),
                        "USDT",
                        self._mode,
                        self._backtest_id,
                        '{"kind":"limit_fill"}',
                    )
                    for f in fills
                ],
                ["fill_id", "ts"],
            )
            async with acquire() as conn:
                for f in fills:
                    await conn.execute(
                        "UPDATE trading.orders "
                        "SET status = 'FILLED', ts_last_update = $1 "
                        "WHERE order_id = $2",
                        _dt(f.ts),
                        f.coid,
                    )
        except Exception as e:
            log.error(
                "limit_book.persist_fills_err",
                n=len(fills),
                err=str(e),
            )


def _json(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, separators=(",", ":"))
