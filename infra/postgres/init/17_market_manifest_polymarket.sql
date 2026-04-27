-- Generalize research.market_manifest_btc15m to research.market_manifest_polymarket
-- with asset_class + horizon columns, supporting BTC + ETH × 5m + 15m
-- (and any future asset/horizon families) under one manifest.
--
-- Migration semantics:
-- 1. Create new table.
-- 2. Copy 599 BTC-15m rows from the old table with asset_class='BTC',
--    horizon='15m'.
-- 3. Drop the old table only AFTER successful copy + index creation.
--
-- The mm_rebate_v1_* strategies query this table by (asset_class, horizon)
-- to gate which markets they quote on.

CREATE TABLE IF NOT EXISTS research.market_manifest_polymarket (
    condition_id         TEXT PRIMARY KEY,
    slug                 TEXT NOT NULL,
    asset_class          TEXT NOT NULL,        -- 'BTC' | 'ETH' | future ('SOL', 'XRP', ...)
    horizon              TEXT NOT NULL,        -- '5m' | '15m'
    open_ts              TIMESTAMPTZ,
    close_ts             TIMESTAMPTZ,
    resolution           TEXT,                 -- 'Up' | 'Down' | NULL (pending)
    has_book_data        BOOLEAN NOT NULL DEFAULT FALSE,
    has_trade_data       BOOLEAN NOT NULL DEFAULT FALSE,
    prices_n_rows        INTEGER NOT NULL DEFAULT 0,
    trades_n_rows        INTEGER NOT NULL DEFAULT 0,
    enumerated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_validated_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS market_manifest_polymarket_close_idx
    ON research.market_manifest_polymarket (close_ts DESC);
CREATE INDEX IF NOT EXISTS market_manifest_polymarket_resolution_idx
    ON research.market_manifest_polymarket (resolution);
CREATE INDEX IF NOT EXISTS market_manifest_polymarket_asset_horizon_idx
    ON research.market_manifest_polymarket (asset_class, horizon, close_ts DESC);

-- Migrate existing BTC-15m rows (599 markets per Step -1.b). Idempotent: if
-- rows already exist in the new table for those condition_ids, ON CONFLICT
-- DO NOTHING preserves the existing entry.
INSERT INTO research.market_manifest_polymarket (
    condition_id, slug, asset_class, horizon, open_ts, close_ts, resolution,
    has_book_data, has_trade_data, prices_n_rows, trades_n_rows,
    enumerated_at, last_validated_at
)
SELECT
    condition_id, slug, 'BTC', '15m', open_ts, close_ts, resolution,
    has_book_data, has_trade_data, prices_n_rows, trades_n_rows,
    enumerated_at, last_validated_at
FROM research.market_manifest_btc15m
ON CONFLICT (condition_id) DO NOTHING;

-- Drop the legacy table once the data has been copied. Safe per the
-- INSERT ... ON CONFLICT above; if the table is empty in this DB, the
-- DROP just removes an empty table.
DROP TABLE IF EXISTS research.market_manifest_btc15m;
