"""Five Binance taker-trade microstructure features over a 90 s window.

Source: ``market_data.crypto_trades`` (exchange='binance' symbol='BTCUSDT',
``side`` ∈ {'buy', 'sell'}; 'buy' is the taker-buy aggressor convention,
matching Binance ``m`` flag inverted).

Feature semantics + sentinels (used identically in training and serving
to avoid train/serve skew):

- ``bm_cvd_normalized`` ∈ [-1, 1]: signed cumulative volume delta /
  total volume. Empty window → 0.0.
- ``bm_taker_buy_ratio`` ∈ [0, 1]: fraction of volume tagged 'buy'.
  Empty window → 0.5 (neutral prior).
- ``bm_trade_intensity`` ≥ 0: window trade count / baseline-trades-per-window
  derived from the trailing 24 h average. Degenerate baseline
  (< 100 trades-per-window) → 1.0.
- ``bm_large_trade_flag`` ∈ {0, 1}: 1 iff any trade with notional
  ``price * qty`` ≥ ``large_threshold_usd`` exists in the window.
- ``bm_signed_autocorr_lag1`` ∈ [-1, 1]: lag-1 autocorrelation of the
  signed-direction series ``s_i = +1 if buy else -1``. Variance-zero
  (constant series) → 0.0; fewer than 10 trades → 0.0.

Modules upstream are responsible for the SQL query that produces the
trade list; this module is pure on the data side and async on the
DB side. See ``estrategias/en-desarrollo/last_90s_forecaster_v3.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC


@dataclass(frozen=True)
class Trade:
    """Minimal trade record consumed by the pure feature functions."""

    price: float
    qty: float
    side: str  # 'buy' or 'sell'


_FEATURE_KEYS: tuple[str, ...] = (
    "bm_cvd_normalized",
    "bm_taker_buy_ratio",
    "bm_trade_intensity",
    "bm_large_trade_flag",
    "bm_signed_autocorr_lag1",
)


def _empty_features() -> dict[str, float]:
    return {
        "bm_cvd_normalized": 0.0,
        "bm_taker_buy_ratio": 0.5,
        "bm_trade_intensity": 1.0,
        "bm_large_trade_flag": 0.0,
        "bm_signed_autocorr_lag1": 0.0,
    }


# ---------- pure functions over a trade list ---------- #


def cvd_normalized(trades: Sequence[Trade]) -> float:
    if not trades:
        return 0.0
    total = sum(t.qty for t in trades)
    if total <= 0.0:
        return 0.0
    cvd = sum(t.qty if t.side == "buy" else -t.qty for t in trades)
    return cvd / total


def taker_buy_ratio(trades: Sequence[Trade]) -> float:
    if not trades:
        return 0.5
    total = sum(t.qty for t in trades)
    if total <= 0.0:
        return 0.5
    return sum(t.qty for t in trades if t.side == "buy") / total


def trade_intensity(n_in_window: int, baseline_trades_24h: int, window_s: int) -> float:
    """``window_s = 90`` → 24 h has 960 windows of 90 s; baseline_per_window
    = baseline_trades_24h / 960. If that average is < 100 we treat the
    24 h sample as too thin to be a useful denominator and return 1.0
    (no signal, neutral)."""
    if baseline_trades_24h <= 0 or window_s <= 0:
        return 1.0
    n_windows_24h = max(1.0, 86400.0 / float(window_s))
    baseline_per_window = baseline_trades_24h / n_windows_24h
    if baseline_per_window < 100.0:
        return 1.0
    return float(n_in_window) / baseline_per_window


def large_trade_flag(trades: Iterable[Trade], threshold_usd: float = 100_000.0) -> float:
    for t in trades:
        if t.price * t.qty >= threshold_usd:
            return 1.0
    return 0.0


def signed_trade_autocorr_lag1(trades: Sequence[Trade], eps: float = 1e-9) -> float:
    if len(trades) < 10:
        return 0.0
    s = [1.0 if t.side == "buy" else -1.0 for t in trades]
    n = len(s)
    mu = sum(s) / n
    centered = [v - mu for v in s]
    var = sum(c * c for c in centered) / n
    if var <= eps:
        return 0.0
    n_pairs = n - 1
    cov = sum(centered[i] * centered[i + 1] for i in range(n_pairs)) / n_pairs
    return cov / var


# ---------- aggregator over a trade list ---------- #


def binance_microstructure_from_trades(
    trades: Sequence[Trade],
    *,
    baseline_trades_24h: int,
    window_s: int = 90,
    large_threshold_usd: float = 100_000.0,
) -> dict[str, float]:
    """All five features in one call. Use this in both training (after
    fetching trades sync via psycopg2) and serving (after async fetch)."""
    return {
        "bm_cvd_normalized": cvd_normalized(trades),
        "bm_taker_buy_ratio": taker_buy_ratio(trades),
        "bm_trade_intensity": trade_intensity(len(trades), baseline_trades_24h, window_s),
        "bm_large_trade_flag": large_trade_flag(trades, threshold_usd=large_threshold_usd),
        "bm_signed_autocorr_lag1": signed_trade_autocorr_lag1(trades),
    }


# ---------- async DB-driven entry point (serving) ---------- #


async def binance_microstructure_features(
    ts,
    *,
    window_s: int = 90,
    large_threshold_usd: float = 100_000.0,
    baseline_trades_24h: int | None = None,
    conn=None,
) -> dict[str, float]:
    """Async-flavored entry point used by the runtime strategy.

    ``ts`` may be a unix-second float or a timezone-aware datetime; the
    SQL uses ``to_timestamp`` either way (we let asyncpg adapt).

    If ``conn`` is None (test path / shadow boot), returns the
    sentinel-only feature dict — no DB access. Callers that want
    "no signal" semantics (e.g. before the first crypto_trades row
    arrives) should rely on this.
    """
    if conn is None:
        return _empty_features()

    from datetime import datetime

    if isinstance(ts, datetime):
        ts_end = ts
    else:
        ts_end = datetime.fromtimestamp(float(ts), tz=UTC)

    rows = await conn.fetch(
        """
        SELECT price::float8, qty::float8, side
        FROM market_data.crypto_trades
        WHERE exchange='binance' AND symbol='BTCUSDT'
          AND ts >= ($1::timestamptz - make_interval(secs => $2))
          AND ts <= $1::timestamptz
        """,
        ts_end,
        window_s,
    )
    trades = [Trade(price=float(r[0]), qty=float(r[1]), side=str(r[2])) for r in rows]

    if baseline_trades_24h is None:
        bl_row = await conn.fetchrow(
            """
            SELECT COUNT(*)::bigint
            FROM market_data.crypto_trades
            WHERE exchange='binance' AND symbol='BTCUSDT'
              AND ts >= ($1::timestamptz - interval '24 hours')
              AND ts <= $1::timestamptz
            """,
            ts_end,
        )
        baseline_trades_24h = int(bl_row[0]) if bl_row and bl_row[0] is not None else 0

    return binance_microstructure_from_trades(
        trades,
        baseline_trades_24h=baseline_trades_24h,
        window_s=window_s,
        large_threshold_usd=large_threshold_usd,
    )


__all__ = [
    "Trade",
    "binance_microstructure_features",
    "binance_microstructure_from_trades",
    "cvd_normalized",
    "large_trade_flag",
    "signed_trade_autocorr_lag1",
    "taker_buy_ratio",
    "trade_intensity",
]
