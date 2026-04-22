-- Phase 4: async backtest jobs launched from API + Telegram bot.

CREATE TABLE IF NOT EXISTS research.backtest_jobs (
    id                    UUID PRIMARY KEY,
    status                TEXT NOT NULL,  -- queued|running|completed|failed|timeout
    strategy_name         TEXT NOT NULL,
    params_file           TEXT NOT NULL,
    data_source           TEXT NOT NULL,
    from_ts               TIMESTAMPTZ NOT NULL,
    to_ts                 TIMESTAMPTZ NOT NULL,
    slug_encodes_open_ts  BOOLEAN NOT NULL DEFAULT FALSE,
    polybot_db_path       TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    finished_at           TIMESTAMPTZ,
    exit_code             INTEGER,
    stdout_tail           TEXT,
    stderr_tail           TEXT,
    backtest_id           UUID,
    error_message         TEXT,
    requested_by          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bt_jobs_status_idx
    ON research.backtest_jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS bt_jobs_strategy_idx
    ON research.backtest_jobs (strategy_name, created_at DESC);
