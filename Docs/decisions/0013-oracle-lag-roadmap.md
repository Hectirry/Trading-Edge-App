# ADR 0013 — `oracle_lag_v1` roadmap (7-sprint plan)

Date: 2026-04-26
Status: Accepted (execution authorized)
Scope: Phase 3.10 — new strategy family branch

## Context

User pasted a long-form analysis of how winning bots operate on Polymarket
BTC up/down 5 m markets (synthesised from the gabagool22 / Archetapp / Jump
public footprint + Saguillo et al. arXiv 2508.03474 + Tsang & Yang
arXiv 2603.03136 + Stoikov 2017 micro-price + Cont-Kukanov-Stoikov 2014
OFI). Headline thesis: **the winner does not predict BTC, it predicts the
next Chainlink Data Streams report.** The mechanism is exploiting (a) the
multi-CEX BTC/USD median that the DON computes vs the single-venue
BTC/USDT last-trade users see, (b) the USDT premium drift, (c) the
sub-second oracle latency vs direct exchange WS.

The hypothesis is captured in
`estrategias/en-desarrollo/oracle_lag_v1.md`. This ADR formalises the
**execution sequence**: 7 sprints with falsifiable kill-switches at the
two highest-cost decision points (Sprint 1 and Sprint 4). It supersedes
nothing — `last_90s_forecaster_v3` and `trend_confirm_t1_v1` remain in
production.

## Decision

### Sequence and gating

```
Sprint 0  ½d   baseline v3 measurement                  (no kill-switch)
Sprint 1  1.5d Phase 0 — analytic Binance-only           ← KILL realized PnL ≤ 0
Sprint 2  2d   Coinbase ingest adapter                   (infra)
Sprint 3  1d   USDT basis stream + table                 (infra)
Sprint 4  2d   Phase 1 — cesta Binance/USDT + Coinbase   ← KILL lift<1pp ∨ pv≥0.05
Sprint 5  3-4d Phase 1.5 — OKX + Kraken                  (optional, conditional)
Sprint 6  ≥2w  paper shadow + promotion gate             (calendar-bound)
Sprint 7  +1w  oracle_lag_v2 maker-first                  (deferred)
```

### Kill-switch criteria (binding)

**Sprint 1** — backtest realized PnL after fees over n ≥ 100 trades:
- `total_pnl ≤ 0` OR permutation p-value ≥ 0.05 → abort the analytic
  approach. The `Φ(δ/σ√τ)` form does not capture an oracle-lag with
  Binance-only data, and adding more venues will not rescue it.

**Sprint 4** — Phase 1 vs Phase 0 comparison:
- Lift in realized PnL bootstrap median < 1 pp **AND**
  permutation p-value ≥ 0.05 → the USDT-basis correction was not what
  the brief implied. Move strategy to `descartadas/` with full Historial
  entry.

**Sprint 5** — incremental venue evaluation:
- Adding OKX → lift < 0.5 pp AUC: drop OKX from cesta.
- Adding Kraken → lift < 0.5 pp AUC: drop Kraken.
- Both fail: cesta stays at Phase 1 weights (0.7 Binance + 0.3 Coinbase).

### Decisions delegated to the executor (operator-approved)

The user explicitly authorised "control total" within this 7-sprint
scope on 2026-04-26. Within scope, the executor decides:

- TOML parameter values within reasonable physical ranges.
- Cesta weights subject to sum=1 and brief alignment.
- Period selection for backtest / MC (must be ≥ 7 days, ≤ available data).
- Whether to run optional walk-forward in Sprint 0 (default: yes, but
  expensive ML retrains may be skipped if budget-bound).
- Naming of helper modules / table columns / metric labels.
- Order of intra-sprint sub-tasks.

### Decisions NOT delegated (require user re-confirmation)

- Promoting `oracle_lag_v1` from `shadow=true` to active
  (`shadow=false`) — that flips real paper PnL exposure and remains
  an explicit ADR-0011-style human gate.
- Killing or pausing any **other** active strategy
  (`last_90s_forecaster_v3`, `trend_confirm_t1_v1`).
- Modifying production secrets or external API auth setup.
- Adding **paid** external data sources (CF Benchmarks, Kaiko,
  Amberdata, etc.).

### Calendar reality (revised 2026-04-26)

Original plan called for ≥ 2 weeks wallclock paper-shadow observation.
Operator pointed out (correctly) that ``market_data.paper_ticks`` has
30 days of retention with ample density — enough to **simulate** the
shadow phase by replay. Sprint 6 therefore becomes a backtest run
against ``--source paper_ticks`` over the last 30 days, with the same
KPIs the wallclock observation would have measured (≥ 50 evaluation
events, divergence vs offline backtest, daily-PnL Sharpe).

This collapses Sprint 6 from ≥ 2 weeks to ≤ 1 day. The trade-off: a
historical replay over paper_ticks does not capture genuine execution
latency under live load — but for a strategy that explicitly evaluates
in T-15→T-3s and depends on book microstructure already captured in
paper_ticks, this is a fair-enough proxy. A real wallclock soak can
still happen post-promotion if the operator wants extra confidence.

## Consequences

- New strategy code path lives next to `trend_confirm_t1_v1` and
  `last_90s_forecaster_v3`. Default `shadow=true` until Sprint 6 passes.
- New ingest adapters (`coinbase`, `okx`, `kraken`) extend the broker
  set declared in Design.md I.5. ADR 0001 (Caddy → Traefik) and ADR
  0008 (multi-strategy registry) remain in force.
- New table `market_data.usdt_basis` (Sprint 3) — TimescaleDB
  hypertable, 30 d retention, follows the pattern of
  `market_data.crypto_ohlcv`.
- Docker compose tea-ingestor adds 3 venue subscriptions across the
  sprint range. Bandwidth impact estimated at +~5-10 MB/min sustained.
- Sprint 7 is a separate ADR if pursued (maker-first quoting touches
  `paper/limit_book_sim.py` rebate model + Avellaneda-Stoikov risk
  model — non-trivial scope expansion).

## References

- Brief (user-supplied 2026-04-26): synthesises gabagool22 / Archetapp
  / @0xngmi public footprint + arXiv 2508.03474 (Saguillo) +
  arXiv 2603.03136 (Tsang & Yang) + Stoikov 2017 SSRN 2970694 +
  Cont-Kukanov-Stoikov 2014.
- `estrategias/en-desarrollo/oracle_lag_v1.md` — full hypothesis,
  variables, falsification.
- ADR 0008 — multi-strategy registry (this strategy follows the same
  per-strategy isolation pattern).
- ADR 0011 — promotion gate (AUC ≥ 0.55 / Brier ≤ 0.245 / ECE ≤ 0.05)
  applies to Sprint 6.
