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
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

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


async def _bulk_ohlcv_opens(conn, window_close_tss: Iterable[int]) -> dict[int, float]:
    """Bulk-fetch window-open strikes for many BTC up/down 5m markets.

    Returns ``{window_close_ts: open_price}``. Markets whose 1m candle is
    missing or has NULL ``open`` are omitted; the caller decides the
    fallback (loader uses the row's ``spot_price``).

    Why 1m + ``open`` and not 5m + ``close``: the 5m candle whose
    ``ts == window_close - 300`` *covers* the window, so its ``close`` is
    the price at window_close, not window_open. That off-by-one strike
    produced sign-flipped ``delta_bps`` and 9.7% win rates on
    paper_ticks backtests (see
    ``estrategias/en-desarrollo/_forensics_trend_confirm_t1_v1.md``).
    """
    close_tss = list(window_close_tss)
    if not close_tss:
        return {}
    open_minutes = [datetime.fromtimestamp(c - 300, tz=UTC) for c in close_tss]
    rows = await conn.fetch(
        "SELECT EXTRACT(EPOCH FROM ts)::bigint AS open_minute, open "
        "FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = ANY($1::timestamptz[])",
        open_minutes,
    )
    by_minute = {int(r["open_minute"]): float(r["open"]) for r in rows if r["open"] is not None}
    return {c: by_minute[c - 300] for c in close_tss if (c - 300) in by_minute}


async def _bulk_settle_prices(conn, slugs: Iterable[str]) -> dict[str, float]:
    """Bulk-fetch canonical settle prices for many paper_ticks markets.

    Returns ``{slug: settle_price}``. Slugs without a matching
    ``polymarket_markets`` row, or without a Binance 1m close at the
    minute-floor of ``close_time``, are omitted — the caller decides
    whether to skip or raise.

    Mirrors ``scripts/backfill_paper_settles.py::_settle_price_at`` (the
    path the production paper engine uses to settle live orders) so
    backtest settle parity holds. Single round-trip via a CTE that joins
    polymarket_markets → crypto_ohlcv on the close-minute.
    """
    slugs_list = list(slugs)
    if not slugs_list:
        return {}
    rows = await conn.fetch(
        """
        WITH m AS (
            SELECT slug, date_trunc('minute', close_time) AS close_minute
            FROM market_data.polymarket_markets
            WHERE slug = ANY($1::text[]) AND close_time IS NOT NULL
        )
        SELECT m.slug, o.close
        FROM m
        JOIN market_data.crypto_ohlcv o
          ON o.exchange = 'binance'
         AND o.symbol = 'BTCUSDT'
         AND o.interval = '1m'
         AND o.ts = m.close_minute
        WHERE o.close IS NOT NULL
        """,
        slugs_list,
    )
    return {r["slug"]: float(r["close"]) for r in rows}


class PaperTicksLoader:
    # Marker attribute consumed by backtest_driver.run_backtest to dispatch
    # the settle path. Polybot's loader does not set this, so its existing
    # `_final_price_of(ticks)` settle path stays untouched.
    provides_settle_prices: bool = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def iter_markets(self, from_ts: float, to_ts: float) -> Iterator[tuple[str, list[TickContext]]]:
        """Yield ``(slug, ticks)`` in ascending first-tick-ts order.

        Single asyncpg connection, two round-trips: one for all ticks in
        the window (ordered by ts), one for all needed window-open
        OHLCV candles. Grouping happens in Python. Replaces a prior
        N+1-connection design (one fresh asyncpg.connect per slug + a
        per-slug OHLCV lookup) that scaled badly past ~50 markets.
        """
        ticks_rows, ohlcv_opens = _run_coro(self._fetch_window(from_ts, to_ts))

        # Group by slug, preserving first-seen order. Since the SELECT
        # is ordered by ts ASC, the first time we see a slug is also its
        # MIN(ts) — so iteration order matches the prior contract
        # (``GROUP BY slug ORDER BY MIN(ts) ASC``).
        by_slug: dict[str, list] = {}
        for r in ticks_rows:
            slug = r["market_slug"]
            if not slug:
                continue
            by_slug.setdefault(slug, []).append(r)

        for slug, rows in by_slug.items():
            window_close_ts = int(rows[0]["window_close_ts"])
            ohlcv_open = ohlcv_opens.get(window_close_ts)
            yield slug, [_row_to_tick(r, ohlcv_open) for r in rows]

    async def _fetch_window(self, from_ts: float, to_ts: float) -> tuple[list, dict[int, float]]:
        conn = await asyncpg.connect(dsn=self.dsn)
        try:
            ticks_rows = await conn.fetch(
                """
                SELECT ts, market_slug, t_in_window, window_close_ts,
                       spot_price, chainlink_price, open_price,
                       pm_yes_bid, pm_yes_ask, pm_no_bid, pm_no_ask,
                       pm_depth_yes, pm_depth_no, pm_imbalance,
                       pm_spread_bps, implied_prob_yes
                FROM market_data.paper_ticks
                WHERE ts >= to_timestamp($1) AND ts <= to_timestamp($2)
                ORDER BY ts ASC
                """,
                from_ts,
                to_ts,
            )
            close_tss = sorted({int(r["window_close_ts"]) for r in ticks_rows})
            ohlcv_opens = await _bulk_ohlcv_opens(conn, close_tss)
        finally:
            await conn.close()
        return ticks_rows, ohlcv_opens

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, float]:
        """Slug → canonical settle price (Binance 1m close at the market's
        ``close_time`` minute). Same source as
        ``scripts/backfill_paper_settles.py``; that path is what production
        uses to settle paper orders, so backtest parity follows.

        Markets without a polymarket_markets row or without an OHLCV
        candle at their close minute are omitted from the dict — the
        caller must decide whether to skip them or raise. Single
        round-trip via a CTE join (replaces a prior N-query loop).
        """

        async def _fetch() -> dict[str, float]:
            conn = await asyncpg.connect(dsn=self.dsn)
            try:
                slug_rows = await conn.fetch(
                    """
                    SELECT DISTINCT market_slug
                    FROM market_data.paper_ticks
                    WHERE ts >= to_timestamp($1) AND ts <= to_timestamp($2)
                    """,
                    from_ts,
                    to_ts,
                )
                slugs = [r["market_slug"] for r in slug_rows if r["market_slug"]]
                return await _bulk_settle_prices(conn, slugs)
            finally:
                await conn.close()

        return _run_coro(_fetch())


def _row_to_tick(r, ohlcv_open: float | None) -> TickContext:
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
    return TickContext(
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
