# ADR 0002 — Include `ts` in trades PK for TimescaleDB

Date: 2026-04-22
Status: Accepted
Scope: Phase 1

## Context

Design.md I.5 declares the following primary keys:

- `market_data.crypto_trades`: `PRIMARY KEY (exchange, symbol, trade_id)`
- `market_data.polymarket_trades`: `PRIMARY KEY (condition_id, tx_hash)`

Both tables are hypertables partitioned by `ts`. TimescaleDB enforces a
rule: every unique index on a hypertable must include the partitioning
column. Applying the DDL as written fails with:

```
ERROR: cannot create a unique index without the column "ts"
       (used in partitioning)
```

## Decision

Append `ts` to the primary key of both tables:

- `crypto_trades`:     `PRIMARY KEY (exchange, symbol, trade_id, ts)`
- `polymarket_trades`: `PRIMARY KEY (condition_id, tx_hash, ts)`

Semantics are unchanged: `trade_id` is unique per `(exchange, symbol)`
and `tx_hash` is globally unique, so adding `ts` cannot admit duplicates
that the original PK would reject. It merely satisfies TimescaleDB's
partitioning invariant.

## Consequences

- Upserts continue to use `ON CONFLICT DO NOTHING` with the full PK
  column list (`exchange, symbol, trade_id, ts` and
  `condition_id, tx_hash, ts`). Adapter code must pass all four/three
  columns when declaring the conflict target.
- The doc-level I.5 schema is now one column wider than printed. This
  ADR is the canonical reference; Design.md retains the narrative form
  and points readers here via this record.
- Query cost is identical: PK index still answers point lookups by
  `trade_id` / `tx_hash`, and TimescaleDB chunk exclusion handles the
  `ts` filtering separately.

## Revisit

No trigger to revisit. The constraint is structural in TimescaleDB and
applies to every hypertable schema we will design in later phases.
