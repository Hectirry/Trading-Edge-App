"""Multi-CEX cesta provider for ``oracle_lag_v1`` (ADR 0013 / 0014).

5 venues:

- **USD-native** (no basis correction): Coinbase ``BTC-USD``,
  Kraken ``XBT/USD``.
- **USDT-denominated** (divide by USDT/USD basis): Binance
  ``BTCUSDT``, Bybit ``BTCUSDT``, OKX ``BTC-USDT``.

Pre-loads minute-resolution closes + implicit USDT basis from
Postgres on construction. ``refresh()`` re-queries the DB for the
last hour; intended to be driven by ``_shared_providers_refresh_loop``
in the paper engine so the cesta stays fresh in live operation.

Per-venue staleness sentinel: 5 min. When a venue's most recent
observation is older than that (or the venue has no data at all)
its weight is redistributed proportionally to the available venues.

ADR 0013 finding (Sprint 5): at 1m granularity OKX adds no edge over
Binance (correlation too high). Including all 5 anyway is an explicit
override decided 2026-04-26 — the cesta provider supports it cleanly,
and the staleness fallback means missing venues don't break paper.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.engine.features.usdt_basis import basis_at


@dataclass(frozen=True)
class CestaWeights:
    binance: float = 0.40   # USDT
    bybit: float = 0.10     # USDT
    coinbase: float = 0.25  # USD nativo
    okx: float = 0.15       # USDT
    kraken: float = 0.10    # USD nativo (a menudo missing por REST cap)

    def normalised(self) -> CestaWeights:
        total = self.binance + self.bybit + self.coinbase + self.okx + self.kraken
        if total <= 0:
            return CestaWeights()
        return CestaWeights(
            binance=self.binance / total,
            bybit=self.bybit / total,
            coinbase=self.coinbase / total,
            okx=self.okx / total,
            kraken=self.kraken / total,
        )


class CestaProvider:
    """Cesta over 5 CEXes + USDT basis correction.

    Series are ascending lists of (ts_unix, close).

    - ``coinbase_series`` — Coinbase ``BTC-USD`` 1m (USD nativo).
    - ``bybit_series`` — Bybit ``BTCUSDT`` 1m (apply basis).
    - ``okx_series`` — OKX ``BTC-USDT`` 1m (apply basis).
    - ``kraken_series`` — Kraken ``XBT/USD`` 1m (USD nativo, often
      empty: REST OHLC capped at ~720 candles, live stream needed).
    - ``basis_series`` — implicit (or pure) USDT/USD basis in 1m.

    Construction does an initial in-memory load. ``refresh(conn)`` is
    async and re-queries the DB; intended for paper-engine
    ``_shared_providers_refresh_loop``. In backtest the static load is
    sufficient — series are pre-fetched once and re-used per tick.
    """

    def __init__(
        self,
        coinbase_series: list[tuple[float, float]],
        basis_series: list[tuple[float, float]],
        bybit_series: list[tuple[float, float]] | None = None,
        okx_series: list[tuple[float, float]] | None = None,
        kraken_series: list[tuple[float, float]] | None = None,
        weights: CestaWeights | None = None,
    ) -> None:
        self._coinbase = list(coinbase_series)
        self._bybit = list(bybit_series or [])
        self._okx = list(okx_series or [])
        self._kraken = list(kraken_series or [])
        self._basis = list(basis_series)
        self._w = (weights or CestaWeights()).normalised()

    @property
    def weights(self) -> CestaWeights:
        return self._w

    @staticmethod
    def _lookup(series: list[tuple[float, float]], ts: float) -> float | None:
        if not series:
            return None
        last_ts: float | None = None
        last_px: float | None = None
        for t, px in series:
            if t > ts:
                break
            last_ts = t
            last_px = px
        if last_ts is None or last_px is None:
            return None
        if (ts - last_ts) > 300:
            return None
        return last_px

    def coinbase_at(self, ts: float) -> float | None:
        return self._lookup(self._coinbase, ts)

    def bybit_at(self, ts: float) -> float | None:
        return self._lookup(self._bybit, ts)

    def okx_at(self, ts: float) -> float | None:
        return self._lookup(self._okx, ts)

    def kraken_at(self, ts: float) -> float | None:
        return self._lookup(self._kraken, ts)

    def basis_at(self, ts: float) -> float:
        return basis_at(ts, self._basis)

    def p_spot(self, ts: float, binance_btcusdt_spot: float) -> tuple[float, dict]:
        """Cesta-weighted BTC/USD spot estimate.

        Binance, Bybit, OKX trade BTC/USDT — divided by USDT basis.
        Coinbase, Kraken trade BTC/USD natively — used as-is. Missing
        venues (≥5 min stale) get their weight redistributed
        proportionally to whatever venues we DO have at this ts.
        """
        basis = self.basis_at(ts)

        binance_usd = binance_btcusdt_spot / basis if basis > 0 else binance_btcusdt_spot
        bybit_usdt = self.bybit_at(ts)
        bybit_usd = (bybit_usdt / basis) if (bybit_usdt and basis > 0) else None
        okx_usdt = self.okx_at(ts)
        okx_usd = (okx_usdt / basis) if (okx_usdt and basis > 0) else None
        coinbase_usd = self.coinbase_at(ts)
        kraken_usd = self.kraken_at(ts)

        weights: list[float] = []
        prices: list[float] = []
        if binance_usd and binance_usd > 0:
            weights.append(self._w.binance)
            prices.append(binance_usd)
        if bybit_usd and bybit_usd > 0:
            weights.append(self._w.bybit)
            prices.append(bybit_usd)
        if coinbase_usd and coinbase_usd > 0:
            weights.append(self._w.coinbase)
            prices.append(coinbase_usd)
        if okx_usd and okx_usd > 0:
            weights.append(self._w.okx)
            prices.append(okx_usd)
        if kraken_usd and kraken_usd > 0:
            weights.append(self._w.kraken)
            prices.append(kraken_usd)

        total_w = sum(weights)
        if total_w <= 0 or not prices:
            p = binance_btcusdt_spot
        else:
            p = sum(w * px for w, px in zip(weights, prices, strict=True)) / total_w

        debug = {
            "basis": basis,
            "binance_usd": binance_usd,
            "bybit_usd": bybit_usd if bybit_usd is not None else float("nan"),
            "coinbase_usd": coinbase_usd if coinbase_usd is not None else float("nan"),
            "okx_usd": okx_usd if okx_usd is not None else float("nan"),
            "kraken_usd": kraken_usd if kraken_usd is not None else float("nan"),
            "p_spot_usd": p,
            "n_venues": len(prices),
        }
        return p, debug

    async def refresh(self, conn) -> None:
        """Re-query the DB for the last 90 min of 1m closes per venue
        + USDT basis. Intended to be called every ~60 s by the paper
        engine's shared-providers refresh loop. Async so it cooperates
        with the existing async loop.
        """
        from trading.engine.features.usdt_basis import load_basis_series

        # 90 min window — keeps the lookup fast and covers any short
        # gap from the ingestor's WS reconnection backoff (max 60 s).
        async def _refresh_one(exchange: str, symbol: str) -> list[tuple[float, float]]:
            rows = await conn.fetch(
                "SELECT EXTRACT(EPOCH FROM ts)::float8 AS ts, close::float8 AS px "
                "FROM market_data.crypto_ohlcv "
                "WHERE exchange=$1 AND symbol=$2 AND interval='1m' "
                "AND ts >= NOW() - INTERVAL '90 minutes' "
                "ORDER BY ts",
                exchange,
                symbol,
            )
            return [(float(r["ts"]), float(r["px"])) for r in rows]

        self._coinbase = await _refresh_one("coinbase", "BTCUSD")
        self._bybit = await _refresh_one("bybit", "BTCUSDT")
        self._okx = await _refresh_one("okx", "BTCUSDT")
        self._kraken = await _refresh_one("kraken", "BTCUSD")
        if self._coinbase:
            basis_from = self._coinbase[0][0]
            basis_to = self._coinbase[-1][0]
            self._basis = await load_basis_series(conn, basis_from, basis_to)


__all__ = ["CestaProvider", "CestaWeights"]
