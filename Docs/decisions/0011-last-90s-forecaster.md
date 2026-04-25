# ADR 0011 — last_90s_forecaster v1 (rules) + v2 (LightGBM)

Date: 2026-04-23
Status: Accepted
Scope: Phase 3.6

## Context

We have two live paper strategies (`imbalance_v3`, `trend_confirm_t1_v1`),
both of which decide in the first half of the 5-minute window. Neither
exploits information produced in the final 90 s before the window
closes. The intuition is simple: directional information accumulates
faster as t approaches 300 s, and the Polymarket `implied_prob_yes`
at t=210 s is often mispriced relative to the micro + macro trend.

This ADR adds two siblings that enter at t ≈ 210 s, 90 s before close:
a rules-based v1 baseline and a LightGBM v2.

## Decision

### Entry timing

Both strategies only consider entries when `t_in_window ∈ [205, 215]`.
Outside that band → `SKIP outside_entry_window`. One entry per market
per strategy (per-market cooldown already enforced by the driver).

### Feature surface (v1 + shared with v2)

Implemented once under `src/trading/engine/features/` and reused by
both strategies plus by the dataset builder:

- `micro.py` — `momentum_bps(spots, lookback_s)`, `realized_vol_yz`,
  `tick_up_ratio`, over the last ≤ 90 s of 1 Hz spot.
- `macro.py` — EMA(8)/EMA(34), ADX(14), consecutive-same-direction
  counter, and a three-way regime classifier (`uptrend`, `downtrend`,
  `range`) over the last 20 Binance 5 m candles.
- `mlofi.py`, `vpin.py`, `microprice.py`, `jumps.py` — added for v2;
  v1 ignores them.

Every feature function accepts an `as_of_ts` (or positionally its
equivalent) and must not peek at data later than that. Unit tests
enforce this with synthetic ticks.

### v1 decision logic

```
if t ∉ [205, 215]:                  SKIP outside_entry_window
if < 60 ticks in last 90 s:         SKIP insufficient_micro_data
if macro snapshot missing:          SKIP no_macro_snapshot
if pm_spread_bps > spread_max_bps:  SKIP spread_too_wide
if macro_regime contradicts micro:  SKIP macro_contradicts_micro
if |edge| < edge_threshold:         SKIP edge_below_threshold
else ENTER YES_UP if edge > 0 else YES_DOWN
```

`micro_prob_up = 0.5 + clamp(momentum_90s / momentum_divisor_bps, -0.45, 0.45)`.
Parameters live in `config/strategies/pbt5m_last_90s_forecaster_v1.toml`
(`momentum_divisor_bps`, `edge_threshold`, `spread_max_bps`, etc.).
The divisor is grid-searched (20/30/40/50/60) on historical polybot
data before paper deploy; the winner is committed to the TOML with the
grid-search report under `research/reports/`.

### v2 — LightGBM

Dataset = combined polybot-btc5m (ro mount) + polybot-agent (ro mount),
normalised via the existing `slug_encodes_open_ts` flag. Expected
~4–4.5 k resolved markets.

Features: the v1 set plus MLOFI (5 levels), VPIN 60 s, Stoikov
microprice, microprice-minus-implied, Lee–Mykland jump flag, and
cyclic encodings for hour / day-of-week. Total ≈ 30.

Label: `close_price > open_price` at t=300 s.

Training: temporal 70/15/15 split, Optuna 200 trials (hard-capped at
1 h), early stopping on val `binary_logloss`. Isotonic calibration
applied when `ECE_val > 0.05`. Reproducibility: fixed `random_state`,
pinned `lightgbm==4.5.0`, git SHA recorded in `meta.json`.

Walk-forward: **refit per fold** (Option B). Two folds on a 6-day
window (4 d IS / 1 d OOS / step 1 d). Small extra cost; prevents
leakage.

Promotion gate (all three must hold on the held-out test set):

- `AUC_test ≥ 0.55`
- `Brier_test ≤ 0.245`
- `ECE_val ≤ 0.05` (post-calibration if applied)

Any failure → shadow mode (`[paper].shadow = true` in the TOML).
Strategy logs `micro_prob` and `edge` per tick but never emits ENTER.

### Model registry

New table `research.models` + a partial-unique index enforcing one
`is_active` row per strategy name. Training CLI writes the row;
strategies read it at boot to resolve the on-disk model path. If no
active row exists → the strategy boots in shadow mode.

### Kelly-fractional sizing

Both v1 and v2 start with `$5` fixed stake. After the **first 20
settled paper trades** (per strategy, in `trading.fills`), the driver
switches to fractional Kelly (¼) capped at `$15/trade`:

```
b = odds_ratio = (1 - implied_prob) / implied_prob    # if ENTER YES_UP
p = micro_prob_up
kelly = max(0, (b*p - (1 - p)) / b)
stake = min(15.0, max(5.0, capital * 0.25 * kelly))
```

This kicks in only when the strategy has empirical data to back the
probability it is emitting; the TOML exposes the thresholds so they
can be tightened.

### Strategy health monitoring

New table `research.strategy_health`: per-strategy periodic snapshots
(WR, Sharpe, AUC_realtime for v2, calibration_drift_score). Populated
by an async task in the driver. Grafana gets a new "Strategy health"
panel and an auto-shadow rule: if `calibration_drift_score > 0.10`
for v2, the strategy flips `shadow=true` without a restart.

## Consequences

- Four paper strategies running in staging; $1000 capital each
  ($4000 total paper).
- tea-engine gains `lightgbm==4.5.0` (~50 MB), `numpy` already present.
- Two new Postgres tables: `research.models`, `research.strategy_health`.
- Training never runs on CI; only on the VPS via the CLI. CI runs the
  v1/v2 golden-trace parity tests against a pinned LGB binary.
- Walk-forward cost: ~2 h per v2 retrain (two Optuna folds). Manual.

## Revisit

- If AUC_realtime drifts > 0.03 from training AUC for > 48 h → retrain
  (manual).
- If either `last_90s_forecaster_v1` or v2 fails its 30-day paper
  gates (v1: Sharpe_per_trade > 0.10 AND WR > 52 %; v2: same + no
  calibration drift), retire.
- If dataset grows past 10 k markets, consider a second v2 variant
  with attention over OFI levels — separate ADR.

## 2026-04-25 — Erratum

Las métricas AUC=0.66 / Brier=0.25 mencionadas arriba se midieron
contra labels contaminadas (ver `_audit_polybot_groundtruth.md` —
40.5 % de las labels que entrenaron `v2_2026-04-23T20-06-38Z` estaban
invertidas vs Binance OHLCV 1 m por chainlink congelado en polybot).
Re-medición contra labels honestas (Binance OHLCV 1m): AUC=0.430.
Modelo `v2_2026-04-23T20-06-38Z` despromovido el 2026-04-25
(`is_active=false`).
