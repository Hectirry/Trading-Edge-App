"""USDT/USD basis helper for ADR 0013 oracle_lag_v1.

The strategy needs to convert Binance / OKX BTC/USDT prices into
BTC/USD-equivalent before aggregating into the multi-CEX cesta. The
true basis would come from a pure stablecoin pair (Coinbase USDC-USDT
or Kraken USDT-USD direct). For Sprint 3 we ship a pragmatic
*implicit* basis instead: the ratio between Coinbase BTC-USD and
Binance BTC-USDT at the same minute. This avoids needing a separate
ingest stream while we still have venue coverage gaps.

Trade-off:
- Implicit basis is high-volume (BTC pairs are deep) but conflates
  "stablecoin drift" with "venue spread / cross-exchange edge". In
  quiet regimes the two are indistinguishable; under stress they
  diverge.
- Pure-stablecoin basis (USDC-USDT) is methodologically cleaner but
  the pair has thin volume on most venues, producing stale or noisy
  ticks.

Sprint 5 brings Kraken USDT-USD direct, at which point the strategy
should switch to the canonical source. Until then, ``basis_at()`` reads
the implicit ratio and falls back to 1.0 when either input is stale or
missing — the same prefer-drop-over-poison policy as the polybot
forensics fix (2026-04-25).
"""

from __future__ import annotations

import math
from collections.abc import Iterable

# Maximum age of an OHLCV minute observation we still trust. 5 min handles
# brief gaps; beyond that the basis is treated as stale → sentinel 1.0.
MAX_STALENESS_S = 300

# Bounds for sane basis values. Anything outside [0.95, 1.05] is almost
# certainly garbage (a 5 % USDT depeg has happened only twice historically
# and would warrant explicit handling, not auto-application).
BASIS_MIN = 0.95
BASIS_MAX = 1.05


def implicit_basis(
    binance_btcusdt_close: float | None,
    coinbase_btcusd_close: float | None,
) -> float | None:
    """Implicit USDT/USD basis from BTC pairs.

    ``basis = coinbase_BTC_USD / binance_BTC_USDT``. Multiplying a USDT
    price by basis gives a USD-equivalent.

    Returns None on any None / non-positive / out-of-bounds input —
    callers fall back to 1.0.
    """
    if (
        binance_btcusdt_close is None
        or coinbase_btcusd_close is None
        or binance_btcusdt_close <= 0
        or coinbase_btcusd_close <= 0
    ):
        return None
    basis = coinbase_btcusd_close / binance_btcusdt_close
    if not math.isfinite(basis):
        return None
    if basis < BASIS_MIN or basis > BASIS_MAX:
        return None
    return basis


def basis_at(ts: float, basis_series: Iterable[tuple[float, float]]) -> float:
    """Look up the most recent basis observation at-or-before ``ts``.

    ``basis_series`` is an iterable of (ts_unix, basis) pairs sorted
    ascending. Returns 1.0 if no observation is within MAX_STALENESS_S
    of the requested ts (sentinel "no correction").

    The strategy hot-path receives a pre-loaded list (via
    ``load_basis_series``); this function is the in-strategy lookup.
    """
    last_ts: float | None = None
    last_basis: float = 1.0
    for t, b in basis_series:
        if t > ts:
            break
        last_ts = t
        last_basis = b
    if last_ts is None:
        return 1.0
    if (ts - last_ts) > MAX_STALENESS_S:
        return 1.0
    return last_basis


async def load_basis_series(
    conn,
    from_ts: float,
    to_ts: float,
) -> list[tuple[float, float]]:
    """Pre-load implicit basis 1-minute series from Postgres.

    Joins ``market_data.crypto_ohlcv`` Binance BTCUSDT 1m and Coinbase
    BTCUSD 1m at the same minute. Out-of-bounds basis values are dropped.
    Result is ascending by ts. Used by backtest paths that need a static
    series — paper/live should use a live cache.
    """
    rows = await conn.fetch(
        """
        SELECT EXTRACT(EPOCH FROM b.ts)::float8 AS ts,
               b.close::float8 AS binance_close,
               c.close::float8 AS coinbase_close
        FROM market_data.crypto_ohlcv b
        JOIN market_data.crypto_ohlcv c
          ON c.ts = b.ts AND c.exchange='coinbase' AND c.symbol='BTCUSD' AND c.interval='1m'
        WHERE b.exchange='binance' AND b.symbol='BTCUSDT' AND b.interval='1m'
          AND b.ts >= to_timestamp($1) AND b.ts <= to_timestamp($2)
        ORDER BY b.ts
        """,
        from_ts,
        to_ts,
    )
    out: list[tuple[float, float]] = []
    for r in rows:
        b = implicit_basis(
            binance_btcusdt_close=r["binance_close"],
            coinbase_btcusd_close=r["coinbase_close"],
        )
        if b is None:
            continue
        out.append((float(r["ts"]), b))
    return out


__all__ = [
    "BASIS_MAX",
    "BASIS_MIN",
    "MAX_STALENESS_S",
    "basis_at",
    "implicit_basis",
    "load_basis_series",
]
