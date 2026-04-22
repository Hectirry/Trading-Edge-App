"""Data loader for backtests.

Phase 2 primary source: polybot-btc5m SQLite (read-only) for parity
testing. Polybot's backtester replays ticks market-by-market (a 5-minute
Polymarket window at a time); we mirror that grouping here so the
Trading-Edge-App driver can reproduce its output bit-for-bit.
"""

from __future__ import annotations

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


class PolybotSQLiteLoader:
    """Read-only iterator over polybot-btc5m's `ticks` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

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
                           implied_prob_yes, model_prob_yes, edge, z_score
                    FROM ticks
                    WHERE market_slug = ?
                      AND ts >= ? AND ts <= ?
                    ORDER BY ts ASC
                    """,
                    (slug, from_ts, to_ts),
                ).fetchall()
                ticks: list[TickContext] = []
                close_ts = float(slug.rsplit("-", 1)[-1])
                open_ts = close_ts - 300.0
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
                        )
                    )
                yield slug, ticks

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, MarketOutcome]:
        """For each market_slug in range, compute open_price (first tick) and
        final_price (Chainlink at window_close_ts, nearest-neighbor fallback)."""
        outcomes: dict[str, MarketOutcome] = {}
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT market_slug,
                       MIN(ts) AS first_ts,
                       MAX(ts) AS last_ts
                FROM ticks
                WHERE ts >= ? AND ts <= ?
                GROUP BY market_slug
                """,
                (from_ts, to_ts),
            ).fetchall()
            for slug, _first_ts, _last_ts in rows:
                try:
                    close_ts = float(slug.rsplit("-", 1)[-1])
                except ValueError:
                    continue
                open_row = c.execute(
                    "SELECT open_price FROM ticks WHERE market_slug=? " "ORDER BY ts ASC LIMIT 1",
                    (slug,),
                ).fetchone()
                final_row = c.execute(
                    "SELECT chainlink_price, spot_price FROM ticks "
                    "WHERE market_slug=? ORDER BY ts DESC LIMIT 1",
                    (slug,),
                ).fetchone()
                if not open_row or not final_row:
                    continue
                open_price = float(open_row[0] or 0.0)
                final_price = float(final_row[0] or final_row[1] or 0.0)
                if open_price == 0.0 or final_price == 0.0:
                    continue
                went_up = final_price > open_price
                outcomes[slug] = MarketOutcome(
                    slug=slug,
                    window_open_ts=close_ts - 300.0,
                    window_close_ts=close_ts,
                    open_price=open_price,
                    final_price=final_price,
                    went_up=went_up,
                )
        return outcomes
