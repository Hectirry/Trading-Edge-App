"""Data loader for backtests.

Phase 2 primary source: polybot-btc5m SQLite (read-only) for parity
testing. Polybot's backtester replays ticks market-by-market (a 5-minute
Polymarket window at a time); we mirror that grouping here so the
Trading-Edge-App driver can reproduce its output bit-for-bit.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from trading.engine.types import TickContext


@dataclass(frozen=True)
class MarketOutcome:
    slug: str
    window_open_ts: float
    window_close_ts: float
    open_price: float
    final_price: float
    went_up: bool


def _build_pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


class PolybotSQLiteLoader:
    """Read-only iterator over polybot-style `ticks` tables.

    Two sibling projects share the schema but embed a different timestamp
    in the slug suffix:

    * ``polybot-btc5m`` → slug = ``btc-updown-5m-{close_ts}``
    * ``BTC-Tendencia-5m`` → slug = ``btc-updown-5m-{open_ts}``

    The distinction matters because strategies gate on ``t_in_window``
    (= ``ts - open_ts``) and the close-vs-open offset is 300 s. Callers
    pass ``slug_encodes_open_ts=True`` when reading BTC-Tendencia.
    """

    # Capability marker consumed by `engine.backtest_driver.run_backtest`
    # to dispatch the canonical settle path. Set in 2026-04-25 deuda
    # follow-up so polybot_sqlite-sourced backtests stop settling against
    # polybot's frozen chainlink (audit POLYBOT SESGADO, 40.5 % invertidas).
    provides_settle_prices: bool = True

    def __init__(self, db_path: str, slug_encodes_open_ts: bool = False) -> None:
        self.db_path = db_path
        self.slug_encodes_open_ts = slug_encodes_open_ts

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def iter_markets(self, from_ts: float, to_ts: float) -> Iterator[tuple[str, list]]:
        """Yield (market_slug, ticks_list) in ascending FIRST-tick-ts order.

        Mirrors polybot's run_backtest, which sorts markets by
        `by_market[s][0]["ts"]` — the timestamp of the first tick recorded
        for each market. This matters when windows overlap (markets can
        open days before their close). A different sort produces a
        different RNG consumption order and therefore different fill-sim
        outcomes.
        """
        with self._connect() as c:
            slug_rows = c.execute(
                """
                SELECT market_slug, MIN(ts) AS first_ts FROM ticks
                WHERE ts >= ? AND ts <= ?
                GROUP BY market_slug
                ORDER BY first_ts ASC
                """,
                (from_ts, to_ts),
            ).fetchall()
            slugs_in_order = [r[0] for r in slug_rows if r[0]]
            for slug in slugs_in_order:
                rows = c.execute(
                    """
                    SELECT ts, market_slug, t_in_window, spot_price, chainlink_price,
                           open_price, pm_yes_bid, pm_yes_ask, pm_no_bid, pm_no_ask,
                           pm_depth_yes, pm_depth_no, pm_imbalance, pm_spread_bps,
                           implied_prob_yes, model_prob_yes, edge, z_score,
                           delta_bps
                    FROM ticks
                    WHERE market_slug = ?
                      AND ts >= ? AND ts <= ?
                    ORDER BY ts ASC
                    """,
                    (slug, from_ts, to_ts),
                ).fetchall()
                ticks: list[TickContext] = []
                slug_ts = float(slug.rsplit("-", 1)[-1])
                if self.slug_encodes_open_ts:
                    open_ts = slug_ts
                    close_ts = slug_ts + 300.0
                else:
                    close_ts = slug_ts
                    open_ts = slug_ts - 300.0
                for r in rows:
                    ts = r[0]
                    yes_bid = r[6] or 0.0
                    yes_ask = r[7] or 0.0
                    # Fallback formulas from polybot _tick_to_ctx — keep same
                    # invariants so bit-exact parity holds even on older rows.
                    no_bid = r[8] if r[8] is not None else max(0.0, 1 - yes_ask)
                    no_ask = r[9] if r[9] is not None else max(0.0, 1 - yes_bid)
                    ticks.append(
                        TickContext(
                            ts=ts,
                            market_slug=r[1],
                            t_in_window=max(0.0, ts - open_ts),
                            window_close_ts=close_ts,
                            spot_price=r[3] or 0.0,
                            chainlink_price=r[4],
                            open_price=r[5] or (r[3] or 0.0),
                            pm_yes_bid=yes_bid,
                            pm_yes_ask=yes_ask,
                            pm_no_bid=no_bid,
                            pm_no_ask=no_ask,
                            pm_depth_yes=r[10] or 0.0,
                            pm_depth_no=r[11] or 0.0,
                            pm_imbalance=r[12] or 0.0,
                            pm_spread_bps=r[13] or 0.0,
                            implied_prob_yes=r[14] or 0.0,
                            model_prob_yes=r[15] or 0.0,
                            edge=r[16] or 0.0,
                            z_score=r[17] or 0.0,
                            vol_regime="unknown",
                            recent_ticks=[],
                            t_to_close=max(0.0, close_ts - ts),
                            delta_bps=float(r[18] or 0.0),
                        )
                    )
                yield slug, ticks

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, float]:
        """Slug → canonical settle price (Binance ``crypto_ohlcv`` 1m
        ``close`` at minute(close_ts)). Same source as
        ``scripts/backfill_paper_settles.py`` and
        ``PaperTicksLoader.market_outcomes`` — keeps both backtest sources
        on identical ground truth.

        Markets without an OHLCV row at their close minute are omitted —
        the driver skips them rather than fall back to polybot chainlink
        (which the 2026-04-25 audit found was 47% frozen).
        """
        import psycopg2

        # Discover slugs in range from polybot's ticks table.
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT DISTINCT market_slug
                FROM ticks
                WHERE ts >= ? AND ts <= ?
                """,
                (from_ts, to_ts),
            ).fetchall()
        if not rows:
            return {}

        # Map slug → close-minute unix; collect minute set for bulk fetch.
        slug_to_minute: dict[str, int] = {}
        minutes: set[int] = set()
        for (slug,) in rows:
            try:
                slug_ts = int(slug.rsplit("-", 1)[-1])
            except ValueError:
                continue
            close_ts = slug_ts + 300 if self.slug_encodes_open_ts else slug_ts
            close_minute = (int(close_ts) // 60) * 60
            slug_to_minute[slug] = close_minute
            minutes.add(close_minute)
        if not minutes:
            return {}

        # Bulk fetch BTCUSDT 1m close for the whole minute range; index
        # by unix-second-of-minute. Single round-trip.
        conn = psycopg2.connect(_build_pg_dsn())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM ts)::bigint, close "
                    "FROM market_data.crypto_ohlcv "
                    "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' "
                    "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                    (min(minutes), max(minutes)),
                )
                close_by_minute = {
                    int(t): float(c) for (t, c) in cur.fetchall() if c is not None
                }
        finally:
            conn.close()

        out: dict[str, float] = {}
        for slug, minute in slug_to_minute.items():
            settle = close_by_minute.get(minute)
            if settle is not None:
                out[slug] = settle
        return out
