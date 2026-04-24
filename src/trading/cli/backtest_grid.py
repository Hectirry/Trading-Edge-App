"""Continuous-mode backtest CLI for grid strategies (Phase 3.8a).

Reads 1m OHLCV from ``market_data.crypto_ohlcv`` and drives a
``ContinuousDriver`` per strategy. Each bar is expanded into a
synthetic tick sequence (O → L → H → C if the bar closed up, else
O → H → L → C) so the in-memory book sees every level crossed within
the bar. ATR-adaptive and DGT variants also receive the ``Bar`` itself
via ``on_bar_1m``.

This is the minimum viable backtester for 3.8a — no slippage model,
maker fills only, constant 0.1% fee. Wider contract with walk-forward /
reporting lands in 3.8b.

Usage:
  python -m trading.cli.backtest_grid \\
    --params config/strategies/grid/grid_dgt_v1_btc.toml \\
    --from 2025-05-01 --to 2026-04-20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tomllib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from trading.common.config import get_settings
from trading.engine.continuous_strategy_base import Bar
from trading.paper.driver_continuous import ContinuousDriver
from trading.paper.limit_book_sim import LimitBookSim, LimitFill
from trading.strategies.grid.grid_atr_adaptive_v1 import GridAtrAdaptiveV1
from trading.strategies.grid.grid_dgt_v1 import GridDgtV1
from trading.strategies.grid.grid_static_v1 import GridStaticV1


class RecordingBook(LimitBookSim):
    """LimitBookSim that retains the full fills history for PnL accounting."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fills_history: list[LimitFill] = []

    async def on_tick(self, *args, **kwargs):
        fills = await super().on_tick(*args, **kwargs)
        self.fills_history.extend(fills)
        return fills


def _fifo_pair_pnl(fills: list[LimitFill]) -> tuple[float, int, float, int]:
    """FIFO-match BUY fills against subsequent SELL fills.

    Returns ``(realized_pnl, pair_count, net_qty, total_fees)``.
    Net qty is the unmatched long inventory at end-of-run (positive = long,
    negative = short; spot strategies shouldn't go negative but the caller
    uses this to flag bugs).
    """
    from collections import deque

    longs: deque = deque()  # (price, qty_remaining)
    realized = 0.0
    pairs = 0
    fees = 0.0
    for f in fills:
        fees += f.fee
        qty = f.qty
        if f.side == "BUY":
            longs.append([f.price, qty])
        else:  # SELL — match against oldest longs
            while qty > 1e-12 and longs:
                lprice, lqty = longs[0]
                take = min(lqty, qty)
                realized += (f.price - lprice) * take
                qty -= take
                lqty -= take
                if lqty <= 1e-12:
                    longs.popleft()
                else:
                    longs[0][1] = lqty
                pairs += 1
            if qty > 1e-12:
                # SELL with no long leg — counts as a short (not spot-valid);
                # track as negative inventory to expose the issue.
                longs.append([f.price, -qty])
    net_qty = sum(q for _, q in longs)
    return realized, pairs, net_qty, fees


STRATEGIES: dict[str, type] = {
    "grid_static_v1": GridStaticV1,
    "grid_dgt_v1": GridDgtV1,
    "grid_atr_adaptive_v1": GridAtrAdaptiveV1,
}


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _instrument_to_binance(instrument_id: str) -> tuple[str, str]:
    # "BTCUSDT.BINANCE" → ("BTCUSDT", "binance")
    sym, _, venue = instrument_id.partition(".")
    return sym, venue.lower()


def _pick_strategy(strategy_id: str) -> type:
    for key, cls in STRATEGIES.items():
        if strategy_id.startswith(key):
            return cls
    raise SystemExit(f"unknown strategy id: {strategy_id}")


async def _bar_stream(
    *,
    conn: asyncpg.Connection,
    symbol: str,
    venue: str,
    from_ts: datetime,
    to_ts: datetime,
) -> AsyncIterator[Bar]:
    """Yield 1m Bars from crypto_ohlcv in chronological order."""
    rows = await conn.fetch(
        """
        SELECT ts, open, high, low, close, volume
        FROM market_data.crypto_ohlcv
        WHERE exchange = $1 AND symbol = $2 AND interval = '1m'
              AND ts >= $3 AND ts < $4
        ORDER BY ts
        """,
        venue,
        symbol,
        from_ts,
        to_ts,
    )
    for r in rows:
        ts = r["ts"].timestamp()
        yield Bar(
            ts_open=ts,
            ts_close=ts + 60.0,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )


async def _drive(
    *,
    driver: ContinuousDriver,
    bars: AsyncIterator[Bar],
    instrument_id: str,
) -> None:
    """Drive strategy from 1m bars. Each bar expands to 4 synthetic ticks
    (O, L, H, C or O, H, L, C depending on direction) and a final on_bar_1m."""
    started = False
    async for bar in bars:
        if not started:
            await driver.start(spot_px=bar.open, ts=bar.ts_open)
            started = True
        if bar.close >= bar.open:
            tick_path = [bar.open, bar.low, bar.high, bar.close]
        else:
            tick_path = [bar.open, bar.high, bar.low, bar.close]
        # Spread 4 ticks across the 60-second bar window.
        for i, px in enumerate(tick_path):
            t = bar.ts_open + (i + 1) * 15.0
            await driver.on_tick(px=px, ts=t)
        await driver.on_bar_1m(bar)
    if started:
        await driver.stop(ts=driver.stats.last_ts or 0.0)


async def _summarize(
    *,
    book: RecordingBook,
    driver: ContinuousDriver,
    strategy,
    start_px: float,
    last_px: float,
    capital_usd: float,
) -> dict:
    from collections import deque

    realized, pairs, net_qty, fees = _fifo_pair_pnl(book.fills_history)
    # Unrealized MTM of the unmatched long inventory at last_px.
    long_q: deque = deque()
    for f in book.fills_history:
        if f.side == "BUY":
            long_q.append([f.price, f.qty])
        else:
            qty = f.qty
            while qty > 1e-12 and long_q:
                lp, lq = long_q[0]
                take = min(lq, qty)
                qty -= take
                lq -= take
                if lq <= 1e-12:
                    long_q.popleft()
                else:
                    long_q[0][1] = lq
    if long_q:
        total_qty = sum(q for _, q in long_q)
        avg_px = sum(p * q for p, q in long_q) / max(total_qty, 1e-12)
        unrealized_mtm = total_qty * (last_px - avg_px)
    else:
        unrealized_mtm = 0.0

    total_pnl = realized - fees + unrealized_mtm
    pct_return = total_pnl / capital_usd if capital_usd else 0.0
    hodl_pct = (last_px - start_px) / start_px if start_px else 0.0

    return {
        "strategy_id": strategy.strategy_id,
        "instrument_id": strategy.instrument_id,
        "variant": strategy.name,
        "ticks": driver.stats.ticks,
        "fills": driver.stats.fills,
        "placed": driver.stats.placed,
        "cancelled": driver.stats.cancelled,
        "resets": driver.stats.resets,
        "errors": driver.stats.errors,
        "pairs_closed": pairs,
        "net_qty_end": round(net_qty, 8),
        "start_px": start_px,
        "last_px": last_px,
        "hodl_pct": round(hodl_pct, 6),
        "realized_pnl": round(realized, 4),
        "unrealized_mtm": round(unrealized_mtm, 4),
        "fees_paid": round(fees, 4),
        "total_pnl": round(total_pnl, 4),
        "pct_return": round(pct_return, 6),
        "capital_usd": capital_usd,
        "center_price": getattr(strategy, "center_price", 0.0),
        "stopped_out": getattr(strategy.state, "stopped_out", False),
        "open_orders_at_end": len(book.snapshot()),
        "reset_gen": getattr(strategy.state, "reset_gen", 0),
    }


async def _run(args: argparse.Namespace) -> None:
    cfg = tomllib.loads(Path(args.params).read_text())
    strategy_id = cfg["strategy_id"]
    instrument_id = cfg["instrument_id"]
    sym, venue = _instrument_to_binance(instrument_id)

    cls = _pick_strategy(strategy_id)
    strategy = cls(cfg)

    fee_bps = float(cfg.get("paper", {}).get("maker_fee_bps", 10.0))
    capital_usd = float(cfg.get("paper", {}).get("capital_usd", 1000.0))
    book = RecordingBook(persist=False, maker_fee_bps=fee_bps)
    driver = ContinuousDriver(strategy=strategy, book=book)

    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.pg_dsn)
    try:
        first_row = await conn.fetchrow(
            """
            SELECT open FROM market_data.crypto_ohlcv
            WHERE exchange = $1 AND symbol = $2 AND interval = '1m'
                  AND ts >= $3 AND ts < $4
            ORDER BY ts LIMIT 1
            """,
            venue,
            sym,
            args.from_ts,
            args.to_ts,
        )
        last_row = await conn.fetchrow(
            """
            SELECT close FROM market_data.crypto_ohlcv
            WHERE exchange = $1 AND symbol = $2 AND interval = '1m'
                  AND ts >= $3 AND ts < $4
            ORDER BY ts DESC LIMIT 1
            """,
            venue,
            sym,
            args.from_ts,
            args.to_ts,
        )
        if not first_row or not last_row:
            raise SystemExit(f"no 1m OHLCV for {sym}@{venue} in window")

        bars = _bar_stream(
            conn=conn, symbol=sym, venue=venue, from_ts=args.from_ts, to_ts=args.to_ts
        )
        await _drive(driver=driver, bars=bars, instrument_id=instrument_id)

        summary = await _summarize(
            book=book,
            driver=driver,
            strategy=strategy,
            start_px=float(first_row["open"]),
            last_px=float(last_row["close"]),
            capital_usd=capital_usd,
        )
    finally:
        await conn.close()

    print(json.dumps(summary, indent=2, default=str))


def main() -> None:
    p = argparse.ArgumentParser(prog="trading.cli.backtest_grid")
    p.add_argument("--params", required=True)
    p.add_argument("--from", dest="from_ts", required=True, type=_parse_date)
    p.add_argument("--to", dest="to_ts", required=True, type=_parse_date)
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
