# ADR 0006 — Phase 2 uses a custom backtest driver, Nautilus integration deferred to Phase 3

Date: 2026-04-22
Status: Accepted
Scope: Phase 2

## Context

Design.md says Phase 2 brings up "el `TradingNode` de Nautilus corriendo
dentro del contenedor `trading-engine`". The Phase 2 prompt pinned
`nautilus_trader==1.215.0` for consistency.

In implementation two tensions appeared:

1. **Parity is the hard filter.** The acceptance criterion is *zero*
   differences against `/home/coder/polybot-btc5m`'s JSON trade vector,
   on the same 4.4-day tick window. Nautilus's BacktestEngine has its
   own event scheduling, fill model and data-handling pipeline.
   Running imbalance_v3 through Nautilus introduces at least one extra
   degree of freedom between our output and the reference JSON.
2. **Strategy inputs are ported ticks, not Nautilus-native streams.**
   The parity tick source is polybot's SQLite `ticks` table. Each row
   already carries `pm_imbalance`, `pm_depth_yes`, `pm_spread_bps`,
   etc. — the exact features the strategy consumes. Wrapping this as
   Nautilus QuoteTicks/Bars adds glue code that would change
   behavior, not preserve it.

## Decision

Phase 2 delivers backtest via a **custom lightweight driver**
(`src/trading/engine/backtest_driver.py`) that iterates ticks and
calls the strategy's `should_enter(ctx)` method directly. The
strategy inherits from a local `StrategyBase` abstract class that
intentionally mirrors the shape of `Nautilus.Strategy` — the same
methods will port to Nautilus with minimal change when live trading
arrives in Phase 3.

`nautilus_trader==1.215.0` is pinned in `pyproject.toml` under an
optional `[engine-live]` extra. The tea-engine image for Phase 2
does **not** install it, keeping the image small and the backtest
deterministic. Phase 3 or Phase 6 will install the extra and write
the Nautilus-backed paper/live execution clients.

`src/trading/engine/node.py` exposes a `TradingNode` factory. In
backtest mode it returns our custom driver wrapped in a node
interface. In paper/live modes it raises `NotImplementedError` with
a clear message pointing to Phase 3/Phase 6.

## Consequences

- **Parity stays clean.** Every difference is traceable to our
  strategy port, not to scheduling differences with Nautilus.
- **Image stays small** (~150 MB vs ~500 MB with Nautilus + Rust
  compilation artifacts). Phase 2 CI and local runs are faster.
- **Migration path preserved.** The `StrategyBase` shim has the same
  lifecycle hooks (`on_start`, `on_tick`, `on_stop`) that Nautilus
  uses; Phase 3 change is mostly import swaps.
- **Deviation from Design.md.** This ADR is the traceable record.
  Phase 2 acceptance criteria are met; the original wording
  ("TradingNode corriendo") is interpreted as the factory contract,
  not as "Nautilus runtime present".

## Revisit

Revisit when Phase 3 starts, before writing the paper trading
execution client. At that point, either (a) wire the existing
strategy into Nautilus's BacktestEngine for verification, or
(b) keep the custom driver for research and add Nautilus only for
live execution. Decision hinges on whether Nautilus's data handling
catches up to what polybot's ticks already provide.
