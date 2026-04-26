-- Monte Carlo evaluation runs for completed backtests.
-- See src/trading/research/monte_carlo.py and Docs/decisions placeholder.
--
-- ``kind`` ∈ {bootstrap, block}:
--   bootstrap — resamples the realized trade vector with replacement.
--               Cheap. Stores realized + percentiles + permutation p-value.
--   block     — re-runs the backtest driver against bootstrap-resampled
--               5-minute Polymarket market windows. Expensive. ``replicates``
--               JSONB carries per-iteration KPIs.
--
-- backtest_id is OPTIONAL — block-bootstrap runs do not always have a
-- single owning backtest row (the realized run may be persisted by a
-- separate command, or skipped entirely with --no-realized).

CREATE TABLE IF NOT EXISTS research.mc_runs (
    id              UUID PRIMARY KEY,
    backtest_id     UUID REFERENCES research.backtests(id) ON DELETE CASCADE,
    strategy_name   TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('bootstrap', 'block')),
    n_iter          INTEGER NOT NULL,
    seed            INTEGER NOT NULL,
    dataset_from    TIMESTAMPTZ,
    dataset_to      TIMESTAMPTZ,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL,
    verdict         TEXT,
    realized        JSONB,
    percentiles     JSONB,
    means           JSONB,
    stds            JSONB,
    permutation_pvalue DOUBLE PRECISION,
    replicates      JSONB,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS mc_runs_by_strategy_idx
    ON research.mc_runs (strategy_name, started_at DESC);
CREATE INDEX IF NOT EXISTS mc_runs_by_backtest_idx
    ON research.mc_runs (backtest_id) WHERE backtest_id IS NOT NULL;
