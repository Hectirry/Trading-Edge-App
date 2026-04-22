"""Postgres-backed DataLoader for `market_data.paper_ticks`.

Same `iter_markets` signature as PolybotSQLiteLoader so run_backtest
consumes it transparently. Uses asyncpg but exposes a sync iterator to
keep the driver call site simple — it opens a sync psycopg-style
connection via asyncpg under the hood? No — we use a sync path via
`psycopg2` would be heavy. Simpler: open an async connection in a
sync generator with asyncio.run for the whole fetch. For this Phase 3
use case (weekly cron), latency is fine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import asyncpg

from trading.common.logging import get_logger
from trading.engine.types import TickContext

log = get_logger(__name__)


class PaperTicksLoader:
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
            finally:
                await conn.close()
            out: list[TickContext] = []
            for r in rows:
                ts = r["ts"].timestamp()
                close_ts = float(r["window_close_ts"])
                out.append(
                    TickContext(
                        ts=ts,
                        market_slug=r["market_slug"],
                        t_in_window=float(r["t_in_window"] or 0.0),
                        window_close_ts=close_ts,
                        spot_price=float(r["spot_price"] or 0.0),
                        chainlink_price=float(r["chainlink_price"] or 0.0) or None,
                        open_price=float(r["open_price"] or r["spot_price"] or 0.0),
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
                    )
                )
            return out

        slugs = asyncio.run(fetch_slugs())
        for slug in slugs:
            yield slug, asyncio.run(fetch_ticks(slug))
