-- Polymarket /prices-history archive (one row per (token_id, ts)).
--
-- Backfilled by scripts/backfill_polymarket_prices_history.py. Used by
-- train_last90s.build_samples to read the *real* implied_prob_yes at
-- the as_of timestamp instead of hardcoding 0.5 (the v2 baseline issue
-- documented in last_90s_forecaster_v2_bbres.md falsified the bb_residual
-- features against synthetic 0.5 priors).
--
-- ts is timestamptz; the polymarket /prices-history endpoint returns
-- unix-second integers — caller converts before insert.

CREATE TABLE IF NOT EXISTS market_data.polymarket_prices_history (
    condition_id TEXT NOT NULL,
    token_id     TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    ts           TIMESTAMPTZ NOT NULL,
    price        NUMERIC(10, 6) NOT NULL,
    PRIMARY KEY (token_id, ts)
);

SELECT create_hypertable(
    'market_data.polymarket_prices_history', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS polymarket_prices_history_market_idx
    ON market_data.polymarket_prices_history (condition_id, outcome, ts DESC);
