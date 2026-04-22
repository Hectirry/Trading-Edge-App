-- Phase 1 tables under market_data schema. Copy of Design.md I.5 verbatim,
-- plus pragmatic indexes for freshness queries and gap-fill lookups.

-- Crypto OHLCV (Binance, Bybit spot).
CREATE TABLE IF NOT EXISTS market_data.crypto_ohlcv (
    exchange  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    interval  TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    open      NUMERIC(20,8) NOT NULL,
    high      NUMERIC(20,8) NOT NULL,
    low       NUMERIC(20,8) NOT NULL,
    close     NUMERIC(20,8) NOT NULL,
    volume    NUMERIC(28,8) NOT NULL,
    PRIMARY KEY (exchange, symbol, interval, ts)
);
SELECT create_hypertable(
    'market_data.crypto_ohlcv', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS crypto_ohlcv_lookup_idx
    ON market_data.crypto_ohlcv (exchange, symbol, interval, ts DESC);

-- Crypto tick trades (stream-only in Phase 1; 90d retention).
-- ts is included in PK because TimescaleDB requires the partitioning column
-- to appear in every unique index. See ADR 0002.
CREATE TABLE IF NOT EXISTS market_data.crypto_trades (
    exchange  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    trade_id  TEXT NOT NULL,
    price     NUMERIC(20,8) NOT NULL,
    qty       NUMERIC(28,8) NOT NULL,
    side      TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, trade_id, ts)
);
SELECT create_hypertable(
    'market_data.crypto_trades', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS crypto_trades_lookup_idx
    ON market_data.crypto_trades (exchange, symbol, ts DESC);
SELECT add_retention_policy(
    'market_data.crypto_trades',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- Polymarket YES/NO token prices.
CREATE TABLE IF NOT EXISTS market_data.polymarket_prices (
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    price         NUMERIC(10,6) NOT NULL,
    PRIMARY KEY (condition_id, token_id, ts)
);
SELECT create_hypertable(
    'market_data.polymarket_prices', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS polymarket_prices_lookup_idx
    ON market_data.polymarket_prices (condition_id, ts DESC);

-- Polymarket trades. ts in PK per TimescaleDB constraint (ADR 0002).
CREATE TABLE IF NOT EXISTS market_data.polymarket_trades (
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    tx_hash       TEXT NOT NULL,
    price         NUMERIC(10,6) NOT NULL,
    size          NUMERIC(28,8) NOT NULL,
    side          TEXT NOT NULL,
    PRIMARY KEY (condition_id, tx_hash, ts)
);
SELECT create_hypertable(
    'market_data.polymarket_trades', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS polymarket_trades_lookup_idx
    ON market_data.polymarket_trades (condition_id, ts DESC);
SELECT add_retention_policy(
    'market_data.polymarket_trades',
    INTERVAL '180 days',
    if_not_exists => TRUE
);

-- Polymarket market registry.
CREATE TABLE IF NOT EXISTS market_data.polymarket_markets (
    condition_id  TEXT PRIMARY KEY,
    slug          TEXT NOT NULL,
    question      TEXT NOT NULL,
    window_ts     BIGINT,
    resolved      BOOLEAN NOT NULL DEFAULT FALSE,
    outcome       TEXT,
    open_time     TIMESTAMPTZ,
    close_time    TIMESTAMPTZ,
    resolve_time  TIMESTAMPTZ,
    metadata      JSONB
);
CREATE INDEX IF NOT EXISTS polymarket_markets_slug_idx
    ON market_data.polymarket_markets (slug);
CREATE INDEX IF NOT EXISTS polymarket_markets_window_idx
    ON market_data.polymarket_markets (window_ts);
