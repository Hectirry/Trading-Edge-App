-- Phase 3.8a — ADR 3.8. Grid-trading infra.
-- Tracks the logical grid state per strategy: one row per level × reset
-- generation. ``client_order_id`` is the link to ``trading.orders`` for
-- the audit trail; status mirrors the order lifecycle locally so queries
-- for "current open grid" don't need a join to a hypertable.

CREATE TABLE IF NOT EXISTS trading.grid_levels (
    strategy_id       TEXT NOT NULL,
    instrument_id     TEXT NOT NULL,
    reset_gen         INTEGER NOT NULL,
    level_idx         INTEGER NOT NULL,
    side              TEXT NOT NULL,                      -- 'BUY' | 'SELL'
    price             NUMERIC(20,8) NOT NULL,
    qty               NUMERIC(28,8) NOT NULL,
    center_price      NUMERIC(20,8) NOT NULL,
    client_order_id   TEXT NOT NULL,
    status            TEXT NOT NULL,                      -- 'PENDING' | 'PLACED' | 'FILLED' | 'CANCELLED'
    placed_at         TIMESTAMPTZ,
    filled_at         TIMESTAMPTZ,
    cancelled_at      TIMESTAMPTZ,
    cancel_reason     TEXT,
    metadata          JSONB,
    PRIMARY KEY (strategy_id, reset_gen, level_idx, side)
);

CREATE INDEX IF NOT EXISTS grid_levels_by_coid_idx
    ON trading.grid_levels (client_order_id);

CREATE INDEX IF NOT EXISTS grid_levels_open_idx
    ON trading.grid_levels (strategy_id, status)
    WHERE status IN ('PENDING', 'PLACED');
