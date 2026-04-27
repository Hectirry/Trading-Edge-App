-- Step 1: persistent state for the rolling k(δ) estimator (mm_rebate_v1).
--
-- The estimator maintains, per (strategy, bucket, delta_cents), a 7-day rolling
-- count of fills observed and minutes-of-quoting, so k(δ) = fills / minutes can
-- be recomputed across restarts. The strategy boots warm from this table with
-- the values committed during Step 0 (research/reports/...step0_mm_rebate_v1.html).
--
-- Pre-paper-ticks-15m TODO (post-Step 0 v2): the `minutes_quoted` accounting is
-- approximate when sourcing from polymarket_prices_history (1-min fidelity);
-- once paper_ticks 15m is populated, switch to true 1Hz wall-clock accounting.

CREATE TABLE IF NOT EXISTS research.k_estimator_state (
    strategy_id    TEXT NOT NULL,
    bucket         TEXT NOT NULL,        -- e.g. '0.15-0.20'
    delta_cents    INTEGER NOT NULL,     -- 1, 2, 3, 5
    window_start   TIMESTAMPTZ NOT NULL, -- start of the rolling window (now-7d)
    fills_count    BIGINT NOT NULL DEFAULT 0,
    minutes_quoted DOUBLE PRECISION NOT NULL DEFAULT 0,
    k_value        DOUBLE PRECISION,     -- fills_count / minutes_quoted (cached)
    last_update    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, bucket, delta_cents)
);

CREATE INDEX IF NOT EXISTS k_estimator_state_strategy_idx
    ON research.k_estimator_state (strategy_id);
CREATE INDEX IF NOT EXISTS k_estimator_state_last_update_idx
    ON research.k_estimator_state (last_update DESC);
