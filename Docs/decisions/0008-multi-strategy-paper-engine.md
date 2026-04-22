# ADR 0008 — Multi-strategy paper engine

Date: 2026-04-22
Status: Accepted
Scope: Phase 3.5+

## Context

Phase 3 wired the paper engine for one strategy (`imbalance_v3`). Phase 3.5
ports a second strategy (`trend_confirm_t1_v1`) from `~/BTC-Tendencia-5m`.
Both strategies consume the same live ticks but reason in orthogonal
ways (orderbook imbalance vs. AFML trend confirmation) and trade at
different horizons (120-240 s vs. 200-220 s in-window). Running both in
the same `tea-engine` container is the intent of Design.md I.3.

Two questions:

1. How to register multiple strategies at boot time.
2. How to keep their capital, cooldowns, daily PnL, and reconciliation
   independent without duplicating the feed / tick recorder / heartbeat
   infrastructure.

## Decision

### Registry

Each enabled strategy is declared in `config/environments/staging.toml`
under `[strategies.<name>]`. At boot, `cli.paper_engine` reads the
registry, resolves each entry to a concrete `StrategyBase` subclass
via an explicit dispatch (`_load_strategy(name, cfg)`), and spawns
one `PaperDriver` per strategy. No dynamic discovery; adding a
strategy requires editing the dispatcher.

### Per-strategy isolation

- **Risk manager**: one `RiskManager` instance per strategy. Capital,
  `daily_loss_alert_pct`, `daily_loss_pause_pct`, `cooldown_seconds`,
  `daily_trade_limit`, and rolling-loss params are read from the
  strategy's TOML config (the same TOML that holds the strategy
  params — keeps strategy-specific risk co-located with the hypothesis).
- **PaperDriver**: one per strategy. Each subscribes to the same
  Redis channel (`tea:paper_ticks`); Redis pub/sub delivers each
  message to every subscriber, so each driver sees the full tick
  stream independently.
- **Client order IDs** already contain the strategy name in the seed
  (`sha256(strategy|slug|ts|side)[:16]`), so a slug can be entered by
  two strategies concurrently without ID collisions.
- **Telegram alerts**: each driver passes its own `strategy_id` into
  the alert text. Dedupe + circuit-breaker state is shared globally at
  the `TelegramClient` level — the goal is to protect the channel from
  alert flood, not to isolate per-strategy noise.
- **Reconciliation**: each driver reconciles its own fills by
  filtering `trading.fills WHERE mode='paper' AND strategy_id=<name>`
  against the driver's in-memory ledger.

### Shared resources

- Feed tasks (`run_binance_spot_1s`, `run_chainlink_rtds`,
  `run_clob_l2`, `refresh_markets_loop`) run once; they populate a
  shared `FeedState`.
- `TickRecorder` runs once; it publishes ticks to Redis at 1 Hz.
- `HeartbeatPublisher` runs once; the heartbeat reflects engine
  liveness, not per-strategy liveness.
- `SimulatedExecutionClient` is instantiated per-strategy (cheap — no
  network resources). Each persists to `trading.orders`/`trading.fills`
  with its own `strategy_id` stamp.

## Consequences

- Adding strategy N+1 is an edit to `_load_strategy` and a new
  `[strategies.<name>]` block. No changes to feeds, tick recorder,
  heartbeat, or the Telegram watcher.
- Memory grows linearly with strategy count: each PaperDriver owns
  its own IndicatorStack dictionary (one per open market).
  Acceptable at the current scale (< 10 strategies).
- Grafana dashboards must filter by `strategy_id` to avoid mixing
  PnLs. The "Paper trading live" dashboard is extended accordingly.
- If one strategy's driver crashes, the others keep running — Redis
  pub/sub is resilient to individual consumer failures.

## Consequences for Phase 6 (live)

When live execution arrives, the same registry pattern plugs in a real
`ExecutionClient` in place of `SimulatedExecutionClient`. Per-strategy
capital budgets carry over. The only Phase 6 change is the execution
path, not the registry topology.

## Revisit

Revisit if we ever run > 10 strategies concurrently, or if a strategy's
resource profile (AFML recompute, orderbook depth tracking) pushes
the shared tick recorder's CPU headroom below 50 %. At that point,
consider sharding drivers into separate processes.
