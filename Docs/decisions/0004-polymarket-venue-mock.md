# ADR 0004 — Custom `PolymarketVenue` mock for NautilusTrader

Date: 2026-04-22
Status: Accepted
Scope: Phase 2+

## Context

NautilusTrader ships adapters for Binance, Bybit, Interactive Brokers
and a few others, but not for Polymarket. Every instrument traded
through a `TradingNode` must belong to a `Venue` with an
`Instrument` registered in its cache — that is how Nautilus routes
orders, looks up tick sizes, and computes fills. The Phase 2
strategy trades binary markets that only exist on Polymarket, so we
need a venue to register them against.

## Decision

Define a module-local venue identifier `POLYMARKET` with a custom
`PolymarketInstrument` factory in `src/trading/engine/venue.py`. The
venue has **no network code**: no REST clients, no WebSockets, no
API keys. It exists purely as a Nautilus bookkeeping target.

- `InstrumentId` format: `btc-updown-5m-<close_ts>-<YES|NO>.POLYMARKET`,
  one-to-one with the `condition_id + token_id` pair we already store
  in `market_data.polymarket_markets`.
- `price_increment`: `0.01` (Polymarket CLOB tick).
- `size_increment`: `1.0` (one share = $1 notional).
- `multiplier`: `1.0`.
- `venue_type`: `EXCHANGE`.

A custom `SimulatedExecutionClient` (see the strategy engine module)
handles backtest fills against this venue with the parabolic
fee model `0.05 * p * (1 - p) * notional` ported from
`polybot-btc5m/core/executor.py`.

## Why mock, not "real" adapter

Phase 2 is backtest-only. A real Polymarket execution client is
Phase 6 material at the earliest, and even then we will keep this
custom venue in place for backtest mode — the real adapter would
plug in alongside.

## Consequences

- Backtests run fully offline against recorded ticks (polybot
  SQLite in parity mode, `market_data` tables otherwise). No
  risk of accidental live orders from backtest or paper runs.
- Future live integration only has to replace the execution
  client; the instrument/venue topology stays the same.
- Nautilus tooling that assumes a specific venue name (for example
  Parquet catalog partitioning by venue) will get the string
  `POLYMARKET`. This is fine because we do not use Parquet.

## Revisit

Revisit if Nautilus ships a first-party Polymarket adapter, or if
the strike-price semantics of Polymarket markets (binary vs. spot)
require a new instrument class rather than the existing
`CurrencyPair` stand-in.
