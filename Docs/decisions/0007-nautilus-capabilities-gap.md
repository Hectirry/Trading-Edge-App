# ADR 0007 — Nautilus capabilities not used in Phase 3; explicit gap list for Phase 6

Date: 2026-04-22
Status: Accepted
Scope: Phase 3 → Phase 6 migration

## Context

ADR 0006 deferred NautilusTrader integration beyond Phase 2 because the
custom backtest driver was the cleanest way to achieve bit-exact
parity against the `polybot-btc5m` reference JSON. Phase 3 (paper
trading) now continues using the same custom driver pattern against
live feeds. `nautilus_trader==1.215.0` remains pinned under the
`engine-live` extra in `pyproject.toml` but is not imported anywhere
in the running code.

The Phase 3 prompt calls this out: "documentá explícito qué
capacidades de Nautilus NO estás usando y por qué (para que Fase 6
sepa qué migrar)". This ADR is that list.

## Decision

Phase 3 does NOT use the following Nautilus capabilities. Phase 6
(live trading) MUST evaluate each one and either adopt it or
explicitly replace it with a documented alternative.

### 1. `TradingNode` lifecycle + message bus

- **Nautilus offers**: centralized message bus (`MessageBus`) with
  typed events (`OrderAccepted`, `PositionOpened`, `Bar`, `TradeTick`,
  `QuoteTick`, etc.), `TradingNode` start/stop orchestration, graceful
  shutdown, event replay.
- **Phase 3 uses**: plain asyncio tasks, Redis pub/sub for heartbeat
  + tick stream, direct method calls between `TickRecorder`,
  `PaperDriver`, `SimulatedExecutionClient`.
- **Phase 6 implication**: live will need reconnection-aware event
  ordering guarantees (e.g., an `OrderAccepted` must be processed
  before its matching `OrderFilled`). Nautilus's message bus already
  handles this; our custom loop does not in the general case. Phase 6
  should port event handling onto `MessageBus` or justify why a
  lighter FIFO queue is sufficient.

### 2. `ExecutionClient` + `ExecEngine`

- **Nautilus offers**: broker-agnostic `ExecutionClient` base; native
  clients for Binance (spot + futures), Bybit, Interactive Brokers;
  order routing; position tracking with P&L calculation;
  cash-ledger management.
- **Phase 3 uses**: `SimulatedExecutionClient` — a 150-line Python
  class that applies the parabolic fee, slippage, fill probability,
  and writes to `trading.orders`/`trading.fills`. No real broker
  connection.
- **Phase 6 implication**: Polymarket does not have a native Nautilus
  adapter. Live will need a `PolymarketExecutionClient` written from
  scratch, plugging into Nautilus's `ExecEngine` interface. This is
  the single largest Phase 6 task. Binance/Bybit adapters for our
  crypto leg, if we add one, are already shipped by Nautilus.

### 3. `DataClient` + live market data adapters

- **Nautilus offers**: broker-native `DataClient` subclasses for
  Binance/Bybit, WebSocket management with heartbeat + auto-reconnect
  + subscription reconciliation, typed data objects (`Bar`,
  `QuoteTick`, `OrderBookDelta`).
- **Phase 3 uses**: our `TickRecorder` — three hand-written asyncio
  WebSocket loops (Binance kline 1s, Polymarket CLOB, Chainlink
  RTDS) that compose into `TickContext` objects, persist to
  `market_data.paper_ticks`, and publish on Redis.
- **Phase 6 implication**: for Polymarket we keep the custom
  WebSocket code (no Nautilus adapter exists). For Binance/Bybit, if
  we ever need higher-fidelity book events, swap to Nautilus's
  `BinanceDataClient`. Phase 3 does not need book depth on crypto.

### 4. `Cache` + state persistence

- **Nautilus offers**: `Cache` layer that stores orders, positions,
  quotes, and bars in a Redis-backed or in-memory store with snapshot
  + recovery semantics, so restart rehydrates the full engine state.
- **Phase 3 uses**: snapshots of `IndicatorStack` and strategy state
  (streak counter, cooldown) persisted to `trading.strategy_state`
  every 60 s. Positions reconstructed from `trading.fills` on
  restart.
- **Phase 6 implication**: the Cache layer provides faster cold start
  and more granular recovery than our coarse 60 s snapshots. Adopt
  if restart latency becomes a problem (Phase 6 needs <10 s cold
  start to avoid missing windows).

### 5. `Portfolio` + multi-instrument risk

- **Nautilus offers**: `Portfolio` actor tracks exposures, P&L,
  margin usage across all instruments under management, with risk
  hooks (`RiskEngine`, margin call simulation, PDT rules).
- **Phase 3 uses**: `RiskManager` ported from polybot — one
  strategy, one instrument family (Polymarket btc-updown-5m), flat
  ledger. Daily loss limit + cooldown + rolling loss window.
- **Phase 6 implication**: when/if we run multiple strategies live,
  our flat ledger breaks. At that point, either extend `RiskManager`
  to be portfolio-aware or adopt Nautilus `Portfolio`. Phase 6 with a
  single strategy can stay on our ledger.

### 6. `BacktestEngine` + Nautilus-native backtesting

- **Nautilus offers**: deterministic `BacktestEngine` with its own
  event loop, fill simulation, slippage models, and a native
  `DataCatalog` (Parquet) for tick storage.
- **Phase 3 uses**: our custom backtest driver (ADR 0006). Used now
  for the weekly paper-vs-backtest comparison too; reads from
  `market_data.paper_ticks` instead of polybot SQLite.
- **Phase 6 implication**: none. Our driver produces bit-exact
  parity and runs in <5 s for a 4.4 d window. Phase 6 can stay on
  this path; switch only if we need Nautilus's more expressive
  exchange simulations (e.g., mid-match priority rules).

### 7. `Reporter` + standard report formats

- **Nautilus offers**: `PerformanceStatistics`, `AnalyticsEngine`,
  tearsheet generator, Monte Carlo sensitivity analyzer.
- **Phase 3 uses**: Jinja2 + Plotly templated HTML in
  `src/trading/research/report.py`. Sharpe audit metrics are ours
  (per-trade, daily, i.i.d. annualized with warning).
- **Phase 6 implication**: same as 6 — keep our generator unless we
  need something Nautilus does out of the box that we do not. The
  Sharpe audit note is a TEA-specific signal, not a Nautilus miss.

### 8. Risk kill-switch + halt semantics

- **Nautilus offers**: `RiskEngine` with `halt_all`/`resume_all`
  hooks, manual kill semantics, audit trail of risk events.
- **Phase 3 uses**: `/etc/trading-system/KILL_SWITCH` file check on
  every tick and before every simulated order submit. File present =
  orders rejected. Audit goes to structured logs.
- **Phase 6 implication**: same file-based kill switch remains; it is
  independent of Nautilus. The `RiskEngine` audit trail is a
  nice-to-have, not a migration blocker.

## Why not adopt Nautilus now

1. **Polymarket adapter is missing** — the largest win Nautilus
   brings (broker-native data + execution clients) does not exist for
   our primary venue. We would write the Polymarket adapter anyway.
2. **Parity is preserved** against the polybot reference without
   Nautilus; introducing a second event loop at Phase 3 risks
   reintroducing the scheduling drift we avoided in Phase 2.
3. **Operational cost** — Nautilus pulls in ~300 MB of native
   dependencies (Rust + PyO3 bindings) per image. Paper-only image
   stays at ~150 MB without it.

## Consequences

- Phase 3 and any future research iteration can extend the custom
  driver without touching Nautilus.
- Phase 6 has a clear checklist: items 1–8 above. Each is annotated
  with whether to adopt Nautilus's version or keep the custom path,
  and why.
- Phase 3 onward, new engine modules should keep their interfaces
  compatible with Nautilus's shape (e.g., `Strategy.on_start`,
  `on_stop`) so Phase 6 can swap implementations underneath.

## Revisit

Revisit at the start of Phase 6, before writing a single line of
the Polymarket live execution client. At that point, go through the
eight items, decide adopt vs. keep, and log a new ADR if any are
flipped.
