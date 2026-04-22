-- Phase 3: paper_ticks (live tick recorder output) + paper_vs_backtest_comparisons.

CREATE TABLE IF NOT EXISTS market_data.paper_ticks (
    ts                TIMESTAMPTZ NOT NULL,
    condition_id      TEXT NOT NULL,
    market_slug       TEXT NOT NULL,
    t_in_window       REAL NOT NULL,
    window_close_ts   BIGINT NOT NULL,
    spot_price        NUMERIC(20,8),
    chainlink_price   NUMERIC(20,8),
    open_price        NUMERIC(20,8),
    pm_yes_bid        NUMERIC(10,6),
    pm_yes_ask        NUMERIC(10,6),
    pm_no_bid         NUMERIC(10,6),
    pm_no_ask         NUMERIC(10,6),
    pm_depth_yes      NUMERIC(20,2),
    pm_depth_no       NUMERIC(20,2),
    pm_imbalance      NUMERIC(10,4),
    pm_spread_bps     REAL,
    implied_prob_yes  NUMERIC(10,6),
    PRIMARY KEY (condition_id, ts)
);
SELECT create_hypertable(
    'market_data.paper_ticks', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
SELECT add_retention_policy(
    'market_data.paper_ticks',
    INTERVAL '30 days',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS paper_ticks_slug_ts_idx
    ON market_data.paper_ticks (market_slug, ts DESC);

CREATE TABLE IF NOT EXISTS research.paper_vs_backtest_comparisons (
    id                UUID PRIMARY KEY,
    strategy_name     TEXT NOT NULL,
    week_start        TIMESTAMPTZ NOT NULL,
    week_end          TIMESTAMPTZ NOT NULL,
    paper_trades      INTEGER NOT NULL,
    backtest_trades   INTEGER NOT NULL,
    paper_pnl         NUMERIC(20,8) NOT NULL,
    backtest_pnl      NUMERIC(20,8) NOT NULL,
    delta_trades_pct  NUMERIC(10,4),
    delta_pnl_pct     NUMERIC(10,4),
    common_trades     INTEGER NOT NULL,
    verdict           TEXT NOT NULL,
    detail            JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pvbc_strategy_week_idx
    ON research.paper_vs_backtest_comparisons (strategy_name, week_start DESC);
