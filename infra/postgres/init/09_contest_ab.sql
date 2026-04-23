-- Phase 3.7 — ADR 0012. Contest A/B + new market_data tables.

CREATE TABLE IF NOT EXISTS market_data.liquidation_clusters (
    ts       TIMESTAMPTZ NOT NULL,
    symbol   TEXT NOT NULL,
    side     TEXT NOT NULL,            -- 'long' | 'short'
    price    NUMERIC(20,8) NOT NULL,
    size_usd NUMERIC(20,2) NOT NULL,
    source   TEXT NOT NULL DEFAULT 'coinalyze',
    PRIMARY KEY (symbol, side, price, ts)
);
SELECT create_hypertable(
    'market_data.liquidation_clusters', 'ts',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE
);
SELECT add_retention_policy(
    'market_data.liquidation_clusters', INTERVAL '7 days', if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS liquidation_clusters_price_idx
    ON market_data.liquidation_clusters (symbol, price);

CREATE TABLE IF NOT EXISTS market_data.chainlink_updates (
    ts         TIMESTAMPTZ NOT NULL,
    feed       TEXT NOT NULL,
    round_id   BIGINT NOT NULL,
    answer     NUMERIC(20,8) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    age_s      REAL NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (feed, round_id)
);
CREATE INDEX IF NOT EXISTS chainlink_updates_feed_ts_idx
    ON market_data.chainlink_updates (feed, ts DESC);

CREATE TABLE IF NOT EXISTS research.contest_ab_weekly (
    week_start          TIMESTAMPTZ NOT NULL,
    strategy_id         TEXT NOT NULL,
    n_windows_total     INTEGER NOT NULL,
    n_predicted         INTEGER NOT NULL,
    n_correct           INTEGER NOT NULL,
    accuracy            NUMERIC(6,4),
    coverage            NUMERIC(6,4),
    adjusted            NUMERIC(6,4),
    ci_lower            NUMERIC(6,4),
    ci_upper            NUMERIC(6,4),
    p_value_vs_baseline NUMERIC(8,6),
    details             JSONB,
    PRIMARY KEY (week_start, strategy_id)
);
