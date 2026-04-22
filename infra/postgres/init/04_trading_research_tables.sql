-- Phase 2 schemas. Trading + research tables, hypertables where high-volume.
-- See ADR 0003 for extensions over Design.md I.5.

----------------------------------------------------------------------
-- trading
----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trading.orders (
    order_id        TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    instrument_id   TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    price           NUMERIC(20,8),
    status          TEXT NOT NULL,
    ts_submit       TIMESTAMPTZ NOT NULL,
    ts_last_update  TIMESTAMPTZ NOT NULL,
    mode            TEXT NOT NULL,
    backtest_id     UUID,
    metadata        JSONB,
    PRIMARY KEY (order_id, ts_submit)
);
SELECT create_hypertable(
    'trading.orders', 'ts_submit',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS orders_by_strategy_idx
    ON trading.orders (strategy_id, ts_submit DESC);
CREATE INDEX IF NOT EXISTS orders_by_backtest_idx
    ON trading.orders (backtest_id) WHERE backtest_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trading.fills (
    fill_id         TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    price           NUMERIC(20,8) NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    liquidity_side  TEXT,
    fee             NUMERIC(20,8),
    fee_currency    TEXT,
    mode            TEXT NOT NULL,
    backtest_id     UUID,
    metadata        JSONB,
    PRIMARY KEY (fill_id, ts)
);
SELECT create_hypertable(
    'trading.fills', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS fills_by_order_idx
    ON trading.fills (order_id, ts DESC);
CREATE INDEX IF NOT EXISTS fills_by_backtest_idx
    ON trading.fills (backtest_id) WHERE backtest_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trading.positions_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    strategy_id     TEXT NOT NULL,
    instrument_id   TEXT NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    avg_price       NUMERIC(20,8),
    unrealized_pnl  NUMERIC(20,8),
    realized_pnl    NUMERIC(20,8),
    mode            TEXT NOT NULL,
    backtest_id     UUID,
    PRIMARY KEY (ts, strategy_id, instrument_id)
);
SELECT create_hypertable(
    'trading.positions_snapshots', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS trading.strategy_state (
    strategy_id TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,
    state       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);

----------------------------------------------------------------------
-- research
----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research.backtests (
    id              UUID PRIMARY KEY,
    strategy_name   TEXT NOT NULL,
    strategy_commit TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    params          JSONB NOT NULL,
    dataset_from    TIMESTAMPTZ NOT NULL,
    dataset_to      TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL,
    metrics         JSONB,
    report_path     TEXT,
    nautilus_version TEXT,
    data_source     TEXT NOT NULL DEFAULT 'tea_postgres'
);
CREATE INDEX IF NOT EXISTS backtests_by_strategy_idx
    ON research.backtests (strategy_name, started_at DESC);

CREATE TABLE IF NOT EXISTS research.backtest_trades (
    backtest_id     UUID NOT NULL REFERENCES research.backtests(id) ON DELETE CASCADE,
    trade_idx       INTEGER NOT NULL,
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    entry_ts        TIMESTAMPTZ NOT NULL,
    entry_price     NUMERIC(20,8) NOT NULL,
    exit_ts         TIMESTAMPTZ,
    exit_price      NUMERIC(20,8),
    pnl             NUMERIC(20,8),
    fees            NUMERIC(20,8),
    strategy_side   TEXT,
    slippage        NUMERIC(20,8),
    t_in_window_s   INTEGER,
    vol_regime      TEXT,
    edge_bps        INTEGER,
    metadata        JSONB,
    PRIMARY KEY (backtest_id, trade_idx)
);
CREATE INDEX IF NOT EXISTS backtest_trades_by_entry_idx
    ON research.backtest_trades (backtest_id, entry_ts);

CREATE TABLE IF NOT EXISTS research.walk_forward_runs (
    id              UUID PRIMARY KEY,
    backtest_id     UUID REFERENCES research.backtests(id) ON DELETE CASCADE,
    strategy_name   TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL,
    verdict         TEXT,
    splits          JSONB,
    summary         JSONB
);
