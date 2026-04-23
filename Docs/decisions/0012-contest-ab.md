# ADR 0012 — Contest A/B: `contest_ensemble_v1` vs `contest_avengers_v1`

Date: 2026-04-23
Status: Accepted
Scope: Phase 3.7

## Context

Moondev contest scores accuracy over the BTC 5-minute Polymarket
resolution, with abstention allowed and predictions made *during* the
window (not before). We already have four paper strategies driving
directional bets; none is structured around selective prediction with
a clear accept/abstain gate.

Phase 3.7 adds two sibling strategies that wrap our existing
machinery with contest-shaped decision logic, run them in paper,
and pick a winner via a pre-registered A/B test.

## Decision

### Strategy A — `contest_ensemble_v1`

ML-classical, regime-aware ensemble.

- **L1 HMM regime** (`hmm_regime_btc5m`) — 4-state Gaussian HMM over
  `[log_return, yz_vol_20]` on Binance 5m candles. Labels
  post-assigned by sorting states on `(mean_return, mean_vol)`:
  highest vol → `high_vol`; low vol + positive mean → `trending_bull`;
  low vol + negative mean → `trending_bear`; else `ranging`.
  12-month training window, refit manual weekly.
- **L2 Candle patterns** — TA-Lib wrappers for CDLDOJI, CDLENGULFING,
  CDLMORNINGSTAR, CDLEVENINGSTAR, CDLHAMMER, CDLSHOOTINGSTAR,
  CDLADVANCEBLOCK on the last 5 × 5m candles and 90s of 1m candles.
- **L3 Multi-checkpoint predictors** at `t ∈ {60, 120, 180, 210, 240, 270}`:
  - Model A — OFI multi-level (Cont-Kukanov-Stoikov, 5 levels)
  - Model B — Stoikov microprice drift last 30s
  - Model C — directional momentum (returns 30/60/90s, ADX-weighted)
  - **Model D — BiLSTM SKIPPED in v1** because the combined polybot +
    paper dataset is < 3 k samples; adding a neural net on top
    guarantees overfit. Re-enable when the dataset crosses 3 k.
- **L4 Meta-combiner** — LightGBM over L1 posteriors + L2 pattern
  signals + L3 outputs + `t_in_window` + hour/dow cyclic encoding +
  `pm_spread_bps` + `implied_prob_yes`. Target `close_price > open_price`.
- **L5 Conformal abstention** — isotonic calibration on the last 2
  weeks of paper predictions; emit only if
  `p_calibrated ∉ (0.375, 0.625)` (`α=0.25`). If no checkpoint
  emits by t=270 s → SKIP.

In `high_vol` regime the strategy **abstains** (v1-A never predicts
in high vol — consistent with v1-B's hard SKIP).

### Strategy B — `contest_avengers_v1`

Information-asymmetry based: oracle lag + liquidation gravity + a
selective hard-threshold gate.

- **L1 Chainlink lag exploiter** (primary signal). Pluggable oracle
  source via `ChainlinkWatcher` abstraction:
  - **Primary**: Chainlink Data Streams (`pm-ds-request.streams.chain.link`)
    if `TEA_CHAINLINK_DATASTREAMS_KEY` is set. This is the same feed
    Polymarket resolves against → cleanest signal.
  - **Fallback**: Polygon EACAggregatorProxy
    `0xc907E116054Ad103354f2D350FD2514433D57F6f` via Alchemy free.
    Proxy feed, not the resolution source; interpret as "oracle
    catching up to Binance".
  - **Degraded**: neither key set → signal disabled (score = 0).
  Adapter writes every read to `market_data.chainlink_updates` so a
  late Data Streams promotion does not lose historical context.
- **L2 Liquidation gravity** — Coinalyze `/liquidation-history` polled
  every 60 s, aggregated into `market_data.liquidation_clusters`.
  Gravity scores at each checkpoint weigh clusters within ±0.3 % of
  spot. Coinalyze free tier (40 req/min) is comfortable at 1 req/min.
- **L3 HMM kill-switch** — reuses L1 HMM. `high_vol` → immediate
  SKIP. `ranging` + recent doji → confidence −0.15. Trend aligned
  with L1 Chainlink signal → confidence +0.10.
- **L4 OFI tie-breaker** — activates only when
  `chainlink_age_s < 3` (oracle synced, no lag edge). Provides
  coverage in ~40 % of windows.
- **L5 Hard threshold** (no conformal, no isotonic). Confidence
  aggregator + gate:
  ```
  confidence = 0.50*chainlink_lag_score
             + 0.25*liquidation_gravity_score (same-sign w/ L1)
             + 0.15*hmm_adjustment
             + 0.10*ofi_score (if chainlink_age < 3)
  ```
  PREDICT if `|confidence| ≥ 0.75`; else wait next checkpoint; at
  t=270 if still < 0.75 → SKIP.

**Graceful degradation**: if Coinalyze is down, `gravity_score = 0`
and the confidence cap is clamped to 0.85 (we never emit full-
confidence PREDICT while missing a component). If Chainlink is down,
`chainlink_lag_score = 0` and v1-B effectively falls back to OFI +
HMM; this typically puts `|confidence|` under the 0.75 gate, so the
strategy abstains more.

### Shared feature modules

```
src/trading/engine/features/
    hmm_regime.py           # HMMRegimeDetector, RegimeState
    candle_patterns.py      # TA-Lib wrappers, PatternSignal
    chainlink_oracle.py     # Watcher abstraction + two impls + null
    liquidation_gravity.py  # cluster selection + gravity_scores
    conformal.py            # IsotonicConformal
```

### Schema

```sql
CREATE TABLE market_data.liquidation_clusters (
    ts       TIMESTAMPTZ NOT NULL,
    symbol   TEXT NOT NULL,
    side     TEXT NOT NULL,
    price    NUMERIC(20,8) NOT NULL,
    size_usd NUMERIC(20,2) NOT NULL,
    source   TEXT NOT NULL DEFAULT 'coinalyze',
    PRIMARY KEY (symbol, side, price, ts)
);
SELECT create_hypertable('market_data.liquidation_clusters','ts',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_retention_policy('market_data.liquidation_clusters',
    INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE market_data.chainlink_updates (
    ts         TIMESTAMPTZ NOT NULL,
    feed       TEXT NOT NULL,
    round_id   BIGINT NOT NULL,
    answer     NUMERIC(20,8) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    age_s      REAL NOT NULL,
    source     TEXT NOT NULL,     -- 'datastreams' | 'eac_polygon'
    PRIMARY KEY (feed, round_id)
);
CREATE INDEX ON market_data.chainlink_updates (feed, ts DESC);

CREATE TABLE research.contest_ab_weekly (
    week_start          TIMESTAMPTZ NOT NULL,
    strategy_id         TEXT NOT NULL,
    n_windows_total     INTEGER NOT NULL,
    n_predicted         INTEGER NOT NULL,
    n_correct           INTEGER NOT NULL,
    accuracy            NUMERIC(6,4),
    coverage            NUMERIC(6,4),
    adjusted            NUMERIC(6,4),
    ci_lower            NUMERIC(6,4),
    ci_upper            NUMERIC(6,4),
    p_value_vs_baseline NUMERIC(8,6),
    details             JSONB,
    PRIMARY KEY (week_start, strategy_id)
);
```

Retention on `liquidation_clusters` is 7 days — the live heatmap
value is < 1 hour old, but we keep one week for backtest replay.

### A/B protocol

- **Primary metric**: accuracy on emitted predictions.
- **Secondary**: `adjusted = coverage × accuracy`. Used only as
  tiebreaker.
- **Power analysis**: two-proportion z-test, `α=0.05`, power 0.80,
  baseline 0.55, MDE 0.05 → **n ≈ 790 per arm**.
- **Commit**: 14 calendar days in paper; at ~60 windows/day × 20 %
  coverage = ~170 predictions/day per arm = ~2400 total. Exceeds the
  power bar.
- **Early stop**: `p < 0.01` AND `n ≥ 300` per arm.
- **Post-winner**: keep both running 2 extra weeks for redundancy,
  then retire the loser.
- **Weekly report**: CLI writes `research.contest_ab_weekly`,
  Grafana renders, Telegram bot sends a Sunday summary with the
  current winner call.

### BiLSTM deferred

Dataset today: polybot-agent 451 resolved markets + TEA paper ~200 →
~650 samples. Training a recurrent net with 21+ features would hit a
noise floor. We promise to re-enable the Model D slot (`l3.bilstm`)
in a follow-up ADR once `len(training_samples) ≥ 3000`.

## Consequences

- tea-engine Dockerfile gains `hmmlearn==0.3.3`, `TA-Lib==0.5.1`
  (+ `libta-lib0` apt), `statsmodels==0.14.4` (for two-proportion z
  test in the weekly report), `web3==7.6.0` (EAC proxy fallback).
- Two new ingest adapters run inside tea-ingestor: Chainlink watcher
  (10-30 s cadence) and Coinalyze watcher (60 s cadence). Both obey
  `aiolimiter` and log request counts.
- Four strategies today → six running in staging paper.
- Weekly Telegram job goes from one summary (Phase 4 daily report)
  to a Sunday Phase-3.7 A/B digest as well.

## Revisit

- Data Streams keys arrive → flip v1-B primary source flag; the row
  history under `source='datastreams'` starts flowing.
- Coinalyze free tier retires or rate-limits harder → move to
  `laevitas` (paid) or synthesise clusters from raw Binance futures
  trades via `market_data.polymarket_trades` analogue.
- BiLSTM trigger (`n_samples ≥ 3000`) → new ADR 0012a adds Model D.
- Winner decided + loser retired → ADR addendum captures the
  post-mortem.
