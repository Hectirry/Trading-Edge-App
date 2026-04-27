-- Step -1.b: persistent manifest of btc-updown-15m markets discovered via Gamma /events.
--
-- Purpose: snapshot of which 15m markets were enumerated for mm_rebate_v1 research,
-- with per-market data-coverage flags so future Step 0 / Step 0 v2 runs are reproducible
-- and we can detect drift between the manifest and the actual data in
-- market_data.polymarket_prices / polymarket_trades.
--
-- Historical note: pre-2026-04-28 there is NO 15m coverage in TEA — the upstream Gamma
-- /markets?slug=... lookup drops archived markets after a few minutes and Gamma /events
-- has no working server-side filter for the 15m series, so backfill is bounded to what
-- /events can return in default ordering (≈6.3 days as of the initial enumerate).
-- Live-tap forward (extended adapter) accumulates from 2026-04-27 onward.

CREATE TABLE IF NOT EXISTS research.market_manifest_btc15m (
    condition_id         TEXT PRIMARY KEY,
    slug                 TEXT NOT NULL,
    open_ts              TIMESTAMPTZ,
    close_ts             TIMESTAMPTZ,
    resolution           TEXT,            -- 'Up' | 'Down' | NULL (pending)
    has_book_data        BOOLEAN NOT NULL DEFAULT FALSE,
    has_trade_data       BOOLEAN NOT NULL DEFAULT FALSE,
    prices_n_rows        INTEGER NOT NULL DEFAULT 0,
    trades_n_rows        INTEGER NOT NULL DEFAULT 0,
    enumerated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_validated_at    TIMESTAMPTZ      -- bumped each time has_*_data flags are recomputed
);

CREATE INDEX IF NOT EXISTS market_manifest_btc15m_close_idx
    ON research.market_manifest_btc15m (close_ts DESC);
CREATE INDEX IF NOT EXISTS market_manifest_btc15m_resolution_idx
    ON research.market_manifest_btc15m (resolution);
