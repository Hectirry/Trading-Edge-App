-- USDT/USD basis time series. Used by oracle_lag_v1 (ADR 0013) to
-- correct Binance/OKX BTC/USDT mids into a BTC/USD-equivalent before
-- aggregating into the multi-CEX cesta.
--
-- Source convention: ``basis = USDT_in_USD``, so a Binance BTC/USDT
-- mid is divided by basis to get BTC/USD. ``basis = 1.0`` means the
-- 1:1 peg holds. Typical drift: 0.9994 ± 0.0010 (5-20 bps premium /
-- discount), spikes to ±50-100 bps under stablecoin stress.
--
-- Source attribution: a single row per (exchange, ts) — multiple
-- venues quote slightly different USDT/USD or USDC/USDT pairs and we
-- want to keep them separate. The helper in
-- ``engine.features.usdt_basis`` averages or picks per-policy.

CREATE TABLE IF NOT EXISTS market_data.usdt_basis (
    exchange    TEXT NOT NULL,
    pair        TEXT NOT NULL,             -- e.g. 'USDT-USD', 'USDC-USDT'
    ts          TIMESTAMPTZ NOT NULL,
    basis       NUMERIC(10,6) NOT NULL,    -- USDT in USD (≈1)
    PRIMARY KEY (exchange, pair, ts)
);

SELECT create_hypertable(
    'market_data.usdt_basis', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- 30-day retention (matches paper_ticks; the basis is high-volume
-- and cheaply re-derivable from spot tickers).
SELECT add_retention_policy(
    'market_data.usdt_basis', INTERVAL '30 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS usdt_basis_by_ts_idx
    ON market_data.usdt_basis (ts DESC);
