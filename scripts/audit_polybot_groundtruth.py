"""Read-only audit: do polybot-agent SQLite labels (used by
``trading.cli.train_last90s``) carry the same kind of structural bias
we found in TEA paper_ticks (chainlink frozen, open_price equal to
chainlink rather than spot)?

What it does:
- Reproduces the resolved-markets reconstruction from
  ``train_last90s._load_resolved_markets`` (no ``markets`` table; uses
  ``trades`` + ``ticks``).
- Reports three checks on the polybot-agent SQLite at
  ``/btc-tendencia-data/polybot-agent.db``:
    (a) open_price vs first-observed chainlink_price per market.
    (b) distinct chainlink_price values across the 5 m window per market.
    (c) ``label_stored = close_price > open_price`` vs
        ``label_canonical = (Binance 1m close at close_minute >
                              Binance 1m close at open_minute)``.
- Stratifies disagreement by distinct-CL bucket and |Δ| in bps.
- Quantifies blast radius on the 507-sample v2 training set.

Read-only. No writes anywhere. Idempotent — print only.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

SQLITE_PATH = "/btc-tendencia-data/polybot-agent.db"
SLUG_ENCODES_OPEN_TS = True  # BTC-Tendencia convention; matches train CLI
WINDOW_SECONDS = 300


def _connect_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


def load_resolved_markets(con: sqlite3.Connection) -> list[dict]:
    """Mirror train_last90s._load_resolved_markets for slug_encodes_open_ts=True."""
    trades = con.execute(
        """
        SELECT DISTINCT market_slug
        FROM trades
        WHERE resolution IN ('win', 'loss')
          AND market_slug LIKE 'btc-%updown-5m-%'
        """
    ).fetchall()
    out: list[dict] = []
    for (slug,) in [(r["market_slug"],) for r in trades]:
        try:
            trailing = int(slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        if SLUG_ENCODES_OPEN_TS:
            open_ts = trailing
            close_ts = trailing + WINDOW_SECONDS
        else:
            close_ts = trailing
            open_ts = trailing - WINDOW_SECONDS
        tick_open = con.execute(
            "SELECT open_price, spot_price, chainlink_price "
            "FROM ticks "
            "WHERE market_slug = ? AND open_price IS NOT NULL AND open_price > 0 "
            "ORDER BY t_in_window ASC LIMIT 1",
            (slug,),
        ).fetchone()
        tick_close = con.execute(
            "SELECT spot_price, chainlink_price "
            "FROM ticks "
            "WHERE market_slug = ? "
            "ORDER BY t_in_window DESC LIMIT 1",
            (slug,),
        ).fetchone()
        if tick_open is None or tick_close is None:
            continue
        open_price = float(tick_open["open_price"])
        first_cl = (
            float(tick_open["chainlink_price"])
            if tick_open["chainlink_price"] is not None
            else None
        )
        close_price = float(tick_close["chainlink_price"] or tick_close["spot_price"] or 0.0)
        last_cl = (
            float(tick_close["chainlink_price"])
            if tick_close["chainlink_price"] is not None
            else None
        )
        if open_price <= 0 or close_price <= 0:
            continue
        out.append(
            {
                "slug": slug,
                "open_ts": open_ts,
                "close_ts": close_ts,
                "open_price": open_price,
                "close_price": close_price,
                "first_chainlink": first_cl,
                "last_chainlink": last_cl,
            }
        )
    return out


def distinct_chainlink_per_market(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(
        """
        SELECT market_slug, COUNT(DISTINCT chainlink_price) AS n_distinct
        FROM ticks
        WHERE chainlink_price IS NOT NULL AND chainlink_price > 0
        GROUP BY market_slug
        """
    ).fetchall()
    return {r["market_slug"]: int(r["n_distinct"]) for r in rows}


async def fetch_ohlcv_minute_closes(
    pg_dsn: str, minute_dts: set[datetime]
) -> dict[datetime, float]:
    """Bulk-fetch 1 m close for a set of minute timestamps."""
    if not minute_dts:
        return {}
    conn = await asyncpg.connect(dsn=pg_dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT ts, close
            FROM market_data.crypto_ohlcv
            WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m'
              AND ts = ANY($1::timestamptz[])
            """,
            list(minute_dts),
        )
    finally:
        await conn.close()
    return {r["ts"]: float(r["close"]) for r in rows if r["close"] is not None}


def _minute_floor(unix_ts: float) -> datetime:
    return datetime.fromtimestamp(int(unix_ts) // 60 * 60, tz=UTC)


def _bps_bucket(bps: float) -> str:
    a = abs(bps)
    if a < 1.0:
        return "[0,1)"
    if a < 5.0:
        return "[1,5)"
    if a < 20.0:
        return "[5,20)"
    if a < 50.0:
        return "[20,50)"
    return "[50,inf)"


def _distinct_cl_bucket(n: int) -> str:
    if n <= 1:
        return "1 (frozen)"
    if n <= 5:
        return "2-5"
    if n <= 20:
        return "6-20"
    return ">20"


def percentiles(values: list[int], pcts: list[int]) -> dict[int, int | float]:
    if not values:
        return {p: 0 for p in pcts}
    s = sorted(values)
    n = len(s)
    out: dict[int, int | float] = {}
    for p in pcts:
        if p <= 0:
            out[p] = s[0]
        elif p >= 100:
            out[p] = s[-1]
        else:
            idx = int(round((p / 100.0) * (n - 1)))
            out[p] = s[idx]
    return out


def main() -> int:
    if not Path(SQLITE_PATH).exists():
        print(f"FATAL: {SQLITE_PATH} not found")
        return 2
    from trading.engine.data_loader import warn_if_polybot_stale

    warn_if_polybot_stale(SQLITE_PATH)
    con = _connect_ro(SQLITE_PATH)
    print(f"# polybot-agent audit\n# source: {SQLITE_PATH}\n")

    # --- (a) open_price vs first chainlink ---------------------------- #
    markets = load_resolved_markets(con)
    n_markets = len(markets)
    n_open_eq_first_cl = 0
    n_open_eq_close = 0
    for m in markets:
        if m["first_chainlink"] is not None and abs(m["open_price"] - m["first_chainlink"]) < 0.01:
            n_open_eq_first_cl += 1
        if abs(m["open_price"] - m["close_price"]) < 0.01:
            n_open_eq_close += 1

    print("## (a) open_price patterns")
    print(f"n_markets resolved         {n_markets}")
    print(
        f"open_price == first_cl     {n_open_eq_first_cl} "
        f"({n_open_eq_first_cl/max(n_markets,1):.1%})"
    )
    print(
        f"open_price == close_price  {n_open_eq_close} " f"({n_open_eq_close/max(n_markets,1):.1%})"
    )
    print()

    # --- (b) distinct chainlink across window ------------------------- #
    distinct_cl = distinct_chainlink_per_market(con)
    distinct_for_resolved = [distinct_cl.get(m["slug"], 0) for m in markets]
    pcts = percentiles(distinct_for_resolved, [10, 50, 90, 99])
    bucket_counts = Counter(_distinct_cl_bucket(n) for n in distinct_for_resolved)
    print("## (b) distinct chainlink_price per market window")
    print(f"n_markets with chainlink samples   {len(distinct_for_resolved)}")
    print(
        f"distinct_cl percentiles  p10={pcts[10]}  p50={pcts[50]}  "
        f"p90={pcts[90]}  p99={pcts[99]}"
    )
    print(
        f"average distinct_cl                "
        f"{sum(distinct_for_resolved)/max(len(distinct_for_resolved),1):.1f}"
    )
    print("buckets:")
    for bk in ("1 (frozen)", "2-5", "6-20", ">20"):
        print(f"  {bk:<12}  {bucket_counts.get(bk, 0)}")
    print()

    # --- (c) label_stored vs label_canonical (Binance OHLCV) ----------- #
    minute_set: set[datetime] = set()
    for m in markets:
        minute_set.add(_minute_floor(m["open_ts"]))
        minute_set.add(_minute_floor(m["close_ts"]))
    pg_dsn = _pg_dsn()

    async def _runner():
        return await fetch_ohlcv_minute_closes(pg_dsn, minute_set)

    closes = asyncio.run(_runner())
    n_eligible = 0
    n_match = 0
    n_disagree = 0
    n_skipped_ohlcv_gap = 0
    rows_for_strat: list[dict] = []
    for m in markets:
        open_min = _minute_floor(m["open_ts"])
        close_min = _minute_floor(m["close_ts"])
        bin_open = closes.get(open_min)
        bin_close = closes.get(close_min)
        if bin_open is None or bin_close is None:
            n_skipped_ohlcv_gap += 1
            continue
        n_eligible += 1
        label_stored = 1 if m["close_price"] > m["open_price"] else 0
        label_canon = 1 if bin_close > bin_open else 0
        bps = (bin_close - bin_open) / bin_open * 10_000.0 if bin_open > 0 else 0.0
        rows_for_strat.append(
            {
                "slug": m["slug"],
                "open_ts": m["open_ts"],
                "close_ts": m["close_ts"],
                "stored": label_stored,
                "canon": label_canon,
                "match": label_stored == label_canon,
                "bps": bps,
                "distinct_cl": distinct_cl.get(m["slug"], 0),
            }
        )
        if label_stored == label_canon:
            n_match += 1
        else:
            n_disagree += 1

    pct_disagree = (n_disagree / n_eligible) if n_eligible else 0.0
    print("## (c) label_stored vs label_canonical (Binance 1m)")
    print(f"n_eligible (OHLCV present at both ends)   {n_eligible}")
    print(f"n_skipped (OHLCV gap)                     {n_skipped_ohlcv_gap}")
    print(f"n_match                                   {n_match}")
    print(f"n_disagree                                {n_disagree}")
    print(f"% disagree                                {pct_disagree:.2%}")
    print()

    # --- stratification ---------------------------------------------- #
    print("## stratification of disagreement")
    print()
    by_cl: Counter[str] = Counter()
    by_cl_total: Counter[str] = Counter()
    by_bps: Counter[str] = Counter()
    by_bps_total: Counter[str] = Counter()
    for r in rows_for_strat:
        cl_bk = _distinct_cl_bucket(r["distinct_cl"])
        bps_bk = _bps_bucket(r["bps"])
        by_cl_total[cl_bk] += 1
        by_bps_total[bps_bk] += 1
        if not r["match"]:
            by_cl[cl_bk] += 1
            by_bps[bps_bk] += 1

    print("by distinct chainlink bucket:")
    print(f"  {'bucket':<12} {'n':>5} {'disagree':>9} {'rate':>6}")
    for bk in ("1 (frozen)", "2-5", "6-20", ">20"):
        n = by_cl_total.get(bk, 0)
        d = by_cl.get(bk, 0)
        rate = d / n if n else 0.0
        print(f"  {bk:<12} {n:>5} {d:>9} {rate:>6.1%}")
    print()
    print("by |bin_close − bin_open| bps bucket:")
    print(f"  {'bucket':<12} {'n':>5} {'disagree':>9} {'rate':>6}")
    for bk in ("[0,1)", "[1,5)", "[5,20)", "[20,50)", "[50,inf)"):
        n = by_bps_total.get(bk, 0)
        d = by_bps.get(bk, 0)
        rate = d / n if n else 0.0
        print(f"  {bk:<12} {n:>5} {d:>9} {rate:>6.1%}")
    print()

    # --- (d) blast radius on training set --------------------------- #
    # Reproduce the time filter the v2 training run used:
    # 2025-11-01 ≤ close_ts ≤ 2026-04-25.
    t_from = datetime(2025, 11, 1, tzinfo=UTC).timestamp()
    t_to = datetime(2026, 4, 25, tzinfo=UTC).timestamp()
    in_train_set = [r for r in rows_for_strat if t_from <= r["close_ts"] <= t_to]
    in_train_disagree = sum(1 for r in in_train_set if not r["match"])
    pct_train = in_train_disagree / len(in_train_set) if in_train_set else 0.0
    print("## (d) blast radius on v2 training period (2025-11-01 → 2026-04-25)")
    print(f"n_train_eligible                          {len(in_train_set)}")
    print(f"n_train_disagree                          {in_train_disagree}")
    print(f"% disagree on training set                {pct_train:.2%}")
    print()

    if pct_train >= 0.05:
        verdict = "POLYBOT SESGADO"
    elif pct_train < 0.02:
        verdict = "POLYBOT LIMPIO"
    else:
        verdict = "AMBIGUO"
    print(f"## verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
