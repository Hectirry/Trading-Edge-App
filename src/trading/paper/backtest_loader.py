"""Postgres-backed DataLoader for `market_data.paper_ticks`.

Same `iter_markets` signature as PolybotSQLiteLoader so run_backtest
consumes it transparently.

Implementation note: `run_backtest` is sync but is usually driven by an
outer `asyncio.run(_run(...))` in the CLI. Nested `asyncio.run` is
forbidden, so this loader uses `asyncio.get_event_loop().run_until_complete`
with a fresh loop per call when there is no running loop, and falls back
to running the coroutine via a dedicated thread when there is.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import asyncpg

from trading.common.logging import get_logger
from trading.engine.types import TickContext

log = get_logger(__name__)


def _run_coro(coro):
    """Execute an async coroutine from a sync context, even when an outer
    event loop is already running (the CLI wraps main in `asyncio.run`)."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        return asyncio.run(coro)
    # Running loop present → run the coroutine on a fresh loop in a thread.
    result: dict = {}

    def _worker():
        new_loop = asyncio.new_event_loop()
        try:
            result["value"] = new_loop.run_until_complete(coro)
        finally:
            new_loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "value" not in result:
        raise RuntimeError("PaperTicksLoader helper thread did not complete")
    return result["value"]


async def _fetch_ohlcv_window_open(conn, window_close_ts: int) -> float | None:
    """Window-open strike for a polymarket BTC up/down 5m market.

    Reads the Binance 1m kline whose ``ts == window_close - 300`` (the
    minute that begins at window_open) and returns its ``open`` field —
    the price at exactly window_open. Returns None when no candle exists
    for that minute (gap in ingest); the caller decides the fallback.

    Why 1m + ``open`` and not 5m + ``close``: see
    ``estrategias/en-desarrollo/_forensics_trend_confirm_t1_v1.md``.
    The 5m candle whose ``ts == window_close - 300`` is the candle that
    *covers* the window, so its ``close`` is the price at window_close,
    not window_open — that off-by-one strike fed a sign-flipped
    ``delta_bps`` and produced 9.7% win rates on paper_ticks backtests.
    """
    row = await conn.fetchrow(
        "SELECT open FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = to_timestamp($1)",
        window_close_ts - 300,
    )
    if row is None or row["open"] is None:
        return None
    return float(row["open"])


async def _fetch_settle_price_for_slug(conn, slug: str) -> float | None:
    """Canonical settle price for a paper_ticks market (BTC up/down 5m).

    Mirrors ``scripts/backfill_paper_settles.py::_settle_price_at`` so
    backtest settle parity with the production paper-ticks settle path:
    Binance 1m close at the minute floor of ``polymarket_markets.close_time``.
    Returns None when the market row or the OHLCV candle is missing —
    the caller decides whether to skip the market or raise.
    """
    mkt = await conn.fetchrow(
        "SELECT close_time FROM market_data.polymarket_markets WHERE slug = $1",
        slug,
    )
    if mkt is None or mkt["close_time"] is None:
        return None
    minute_dt = mkt["close_time"].replace(second=0, microsecond=0)
    row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = $1",
        minute_dt,
    )
    if row is None or row["close"] is None:
        return None
    return float(row["close"])


class PaperTicksLoader:
    # Marker attribute consumed by backtest_driver.run_backtest to dispatch
    # the settle path. Polybot's loader does not set this, so its existing
    # `_final_price_of(ticks)` settle path stays untouched.
    provides_settle_prices: bool = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def iter_markets(self, from_ts: float, to_ts: float) -> Iterator[tuple[str, list[TickContext]]]:
        async def fetch_slugs() -> list[str]:
            conn = await asyncpg.connect(dsn=self.dsn)
            try:
                rows = await conn.fetch(
                    """
                    SELECT market_slug, MIN(ts) AS first_ts
                    FROM market_data.paper_ticks
                    WHERE ts >= to_timestamp($1) AND ts <= to_timestamp($2)
                    GROUP BY market_slug ORDER BY first_ts ASC
                    """,
                    from_ts,
                    to_ts,
                )
            finally:
                await conn.close()
            return [r["market_slug"] for r in rows if r["market_slug"]]

        async def fetch_ticks(slug: str) -> list[TickContext]:
            conn = await asyncpg.connect(dsn=self.dsn)
            try:
                rows = await conn.fetch(
                    """
                    SELECT ts, market_slug, t_in_window, window_close_ts,
                           spot_price, chainlink_price, open_price,
                           pm_yes_bid, pm_yes_ask, pm_no_bid, pm_no_ask,
                           pm_depth_yes, pm_depth_no, pm_imbalance,
                           pm_spread_bps, implied_prob_yes
                    FROM market_data.paper_ticks
                    WHERE market_slug = $1
                      AND ts >= to_timestamp($2) AND ts <= to_timestamp($3)
                    ORDER BY ts ASC
                    """,
                    slug,
                    from_ts,
                    to_ts,
                )
                ohlcv_open: float | None = None
                if rows:
                    window_close_ts = int(rows[0]["window_close_ts"])
                    ohlcv_open = await _fetch_ohlcv_window_open(conn, window_close_ts)
            finally:
                await conn.close()
            out: list[TickContext] = []
            for r in rows:
                ts = r["ts"].timestamp()
                close_ts = float(r["window_close_ts"])
                spot = float(r["spot_price"] or 0.0)
                if ohlcv_open is not None and ohlcv_open > 0:
                    open_price = ohlcv_open
                else:
                    open_price = float(r["open_price"] or r["spot_price"] or 0.0)
                delta_bps = 0.0
                if open_price > 0:
                    delta_bps = (spot - open_price) / open_price * 10000.0
                out.append(
                    TickContext(
                        ts=ts,
                        market_slug=r["market_slug"],
                        t_in_window=float(r["t_in_window"] or 0.0),
                        window_close_ts=close_ts,
                        spot_price=spot,
                        chainlink_price=float(r["chainlink_price"] or 0.0) or None,
                        open_price=open_price,
                        pm_yes_bid=float(r["pm_yes_bid"] or 0.0),
                        pm_yes_ask=float(r["pm_yes_ask"] or 0.0),
                        pm_no_bid=float(r["pm_no_bid"] or 0.0),
                        pm_no_ask=float(r["pm_no_ask"] or 0.0),
                        pm_depth_yes=float(r["pm_depth_yes"] or 0.0),
                        pm_depth_no=float(r["pm_depth_no"] or 0.0),
                        pm_imbalance=float(r["pm_imbalance"] or 0.0),
                        pm_spread_bps=float(r["pm_spread_bps"] or 0.0),
                        implied_prob_yes=float(r["implied_prob_yes"] or 0.0),
                        model_prob_yes=0.0,
                        edge=0.0,
                        z_score=0.0,
                        vol_regime="unknown",
                        recent_ticks=[],
                        t_to_close=max(0.0, close_ts - ts),
                        delta_bps=delta_bps,
                    )
                )
            return out

        slugs = _run_coro(fetch_slugs())
        for slug in slugs:
            yield slug, _run_coro(fetch_ticks(slug))

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, float]:
        """Slug → canonical settle price (Binance 1m close at the market's
        ``close_time`` minute). Same source as
        ``scripts/backfill_paper_settles.py``; that path is what production
        uses to settle paper orders, so backtest parity follows.

        Markets without a polymarket_markets row or without an OHLCV
        candle at their close minute are omitted from the dict — the
        caller must decide whether to skip them or raise.
        """

        async def _fetch() -> dict[str, float]:
            conn = await asyncpg.connect(dsn=self.dsn)
            try:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT market_slug
                    FROM market_data.paper_ticks
                    WHERE ts >= to_timestamp($1) AND ts <= to_timestamp($2)
                    """,
                    from_ts,
                    to_ts,
                )
                out: dict[str, float] = {}
                for r in rows:
                    slug = r["market_slug"]
                    if not slug:
                        continue
                    settle = await _fetch_settle_price_for_slug(conn, slug)
                    if settle is not None:
                        out[slug] = settle
                return out
            finally:
                await conn.close()

        return _run_coro(_fetch())
