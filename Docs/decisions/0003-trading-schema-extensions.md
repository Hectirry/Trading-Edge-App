# ADR 0003 — `trading` and `research` schema extensions for Phase 2

Date: 2026-04-22
Status: Accepted
Scope: Phase 2

## Context

Design.md I.5 defines `trading.orders`, `trading.fills`,
`trading.positions_snapshots` and `research.backtests`,
`research.backtest_trades`. Those columns are enough to store *a*
backtest, but the Phase 2 reports borrow the segmentation breakdown
from `polybot-btc5m` (by-hour, by-edge-bps bucket, by-time-in-window,
by-vol-regime) so the two implementations can be compared side by
side. We also need persistent streak / cooldown state for strategies
that survive engine restarts.

## Decision

Extend I.5 with these additions; all are additive and do not change
existing column semantics.

### `trading.strategy_state` (new)

```sql
CREATE TABLE trading.strategy_state (
    strategy_id TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,
    state       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
```

Persists streak counters, cooldown end timestamps, and similar
per-strategy bookkeeping. Hydrated on startup; absence means cold
start. Not a hypertable (one row per strategy).

### `research.backtest_trades` extensions

Columns added to the I.5 baseline:

| column            | type            | why |
|-------------------|-----------------|-----|
| strategy_side     | TEXT            | "YES_UP" / "YES_DOWN" for Polymarket; identifies long-only bias |
| slippage          | NUMERIC(20,8)   | slippage in bps captured at fill time |
| t_in_window_s     | INTEGER         | seconds from window open to entry; drives the `t_in_window` histogram |
| vol_regime        | TEXT            | "low" / "mid" / "high" for the heatmap_hour_vol breakdown |
| edge_bps          | INTEGER         | edge at entry in bps, bucketed in the report |

The primary key stays `(backtest_id, trade_idx)` as per I.5.

## Consequences

- Reports can render the `polybot-btc5m` segmentation tables without
  JOIN gymnastics or JSONB queries at read time. The segmentation
  columns are denormalized from per-tick context intentionally —
  they are immutable once a trade closes.
- Parity tests can compare the five new columns against
  `polybot-btc5m/core/backtest.py` output JSONs directly.
- Writers in Phase 2 must populate these new columns; later phases
  that add strategies must also populate them or document why they
  are `NULL` (e.g., a non-Polymarket strategy has no `t_in_window_s`).

## Revisit

Revisit if a future strategy produces a segmentation dimension that
is not covered here. Add a column per ADR rather than overloading
`metadata`.
