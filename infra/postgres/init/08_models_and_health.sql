-- Phase 3.6: model registry + per-strategy health (ADR 0011).

CREATE TABLE IF NOT EXISTS research.models (
    id                    UUID PRIMARY KEY,
    name                  TEXT NOT NULL,        -- e.g. 'last_90s_forecaster_v2'
    version               TEXT NOT NULL,        -- 'v2_2026-04-23T04-20-00Z'
    path                  TEXT NOT NULL,        -- models/<name>/<version>/
    metrics               JSONB NOT NULL,
    params                JSONB NOT NULL,
    training_period_from  TIMESTAMPTZ,
    training_period_to    TIMESTAMPTZ,
    git_sha               TEXT,
    trained_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active             BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS models_name_version_uk
    ON research.models (name, version);
CREATE INDEX IF NOT EXISTS models_name_trained_idx
    ON research.models (name, trained_at DESC);
-- Exactly one active row per name.
CREATE UNIQUE INDEX IF NOT EXISTS models_active_uk
    ON research.models (name) WHERE is_active;


CREATE TABLE IF NOT EXISTS research.strategy_health (
    ts                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id               TEXT NOT NULL,
    window_hours              INTEGER NOT NULL,
    n_trades                  INTEGER NOT NULL,
    win_rate                  NUMERIC(6,4),
    avg_pnl_usd               NUMERIC(12,6),
    sharpe_per_trade          NUMERIC(8,4),
    auc_realtime              NUMERIC(6,4),             -- v2 only; NULL for v1
    calibration_drift_score   NUMERIC(8,4),             -- v2 only
    details                   JSONB,
    PRIMARY KEY (strategy_id, ts)
);
SELECT create_hypertable(
    'research.strategy_health', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);
