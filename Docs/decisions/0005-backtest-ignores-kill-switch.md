# ADR 0005 — Backtest mode ignores the kill switch

Date: 2026-04-22
Status: Accepted
Scope: Phase 2+

## Context

Design.md I.7 states: *"Kill switch físico. Archivo
`/etc/trading-system/KILL_SWITCH` en el VPS. Si existe, el
`trading-engine` se niega a enviar órdenes. Revisado en cada tick y
al arranque."*

That invariant protects live capital. Backtest mode is offline,
deterministic, and never submits an order to a broker; it writes to
`trading.orders` / `trading.fills` with `mode='backtest'` and
`backtest_id=<uuid>`. Applying the kill switch in backtest would
mean a VPS-level "no live trading" toggle also pauses every research
pipeline, which is nonsense — and someone running a walk-forward
study would not think to check the switch.

## Decision

The kill switch applies to `mode in ('paper', 'live')` **only**.

- `src/trading/engine/node.py` reads
  `/etc/trading-system/KILL_SWITCH` at node construction. If the
  file exists and the mode is paper or live, the engine refuses to
  start (`RuntimeError("KILL_SWITCH active")`).
- In backtest mode the file's existence is logged as an `info` event
  ("kill_switch.present.ignored_in_backtest") and otherwise
  ignored.
- During paper/live runs the switch is re-checked on every tick. A
  live run observing the file mid-session cancels all open orders
  and shuts down the strategy cleanly.

## Consequences

- Research can continue unaffected when an operator sets the kill
  switch to stop real trading. This is the expected behavior.
- The runbook documents this explicitly so no one is surprised when
  a backtest runs successfully with the switch armed.
- CI and automated parity runs (which are backtest-only) are never
  gated on the switch file's presence.

## Revisit

If a future mode (e.g., "paper-with-real-orders-for-reconciliation")
blurs the line between simulation and live, revisit and add the
mode to the kill-switch scope explicitly.
