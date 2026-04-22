# ADR 0009 — Dashboard + Prometheus + node_exporter (Phase 4)

Date: 2026-04-22
Status: Accepted
Scope: Phase 4+

## Context

Phase 4 (per Design.md) delivers three surfaces of human control that
did not exist through Phase 3.5:

1. A **custom research dashboard** (FastAPI + HTMX + Alpine.js) for
   listing backtests, launching new ones, and comparing up to 3
   side-by-side.
2. A **Grafana "System health" panel** showing VPS CPU/RAM/disk,
   WebSocket latency, container uptime, and 24-hour error counts.
3. An **interactive Telegram bot** with nine commands covering
   status/positions/trades/pnl/pause/resume/killswitch/backtest/help.

The Phase 4 prompt also revisits a Phase 0 prohibition:

> No agregues observabilidad con Prometheus/Loki/Jaeger todavía.
> Grafana + logs Docker alcanza para Fase 0.

Phase 0 was the right scope at the time; "System health" now
requires host metrics (CPU/RAM/disk) that Grafana alone cannot
produce without an exporter.

## Decision

### Prometheus + node_exporter revival

Enable two new containers:

- `tea-node-exporter` — image `prom/node-exporter:v1.8.2`,
  `network_mode: host`, read-only bind mounts on `/proc`, `/sys`,
  `/` so it can report kernel + filesystem counters. Listens on
  `127.0.0.1:9100` only; not reachable from outside the host.
- `tea-prometheus` — image `prom/prometheus:v3.1.0`, scrapes
  node_exporter (via the host) plus the existing
  `tea-ingestor:9000/metrics` endpoint. Retention 30 days, storage
  volume `tea_prom_data`. Grafana gains a provisioned Prometheus
  datasource.

This explicitly **reverts the Phase 0 prohibition**. The rationale
has shifted: Phase 0 wanted a small footprint before the engine
existed; Phase 4 needs host visibility to know when the engine is
starved, and the cost of one scraper + one exporter is bounded.

### Dashboard stack

- `tea-api` upgrades from stub to a real FastAPI service
  (`tea-api:0.4.0`). Jinja2 templates render server-side HTML;
  HTMX 2.0 + Alpine 3 come from CDN; Tailwind from CDN. No bundler,
  no `npm` in the image.
- `multiprocessing.Process` per backtest job (no Celery) writes
  status + stdout/stderr tails into `research.backtest_jobs`.
- Auth: `X-TEA-Token` header or `tea_token` cookie; middleware
  rejects 401 otherwise. Token lives in
  `/etc/trading-system/secrets.env` as `TEA_API_TOKEN`, generated
  once via `openssl rand -hex 32`.
- Dashboard routed through the existing Traefik at
  `https://187-124-130-221.nip.io/research/*` (same hostname as
  Grafana; different path prefix).

### Dual-path KILL_SWITCH

The API container cannot write to `/etc/trading-system/KILL_SWITCH`
(mounted read-only). Adding sudo to the API container is a larger
attack surface than the benefit. Introduce a second file path:

- `/etc/trading-system/KILL_SWITCH` — host-managed, sudo-only.
- `/var/tea/control/KILL_SWITCH` — bind-mounted `tea_control`
  volume shared between `tea-api` (read/write) and `tea-engine`
  (read-only).

Engine code checks `any(Path(p).exists() for p in KILL_SWITCH_PATHS)`.
Operators can still touch the `/etc` file via SSH; the Telegram bot
(via the API) touches the `/var/tea` one.

### Pause/resume protocol

1. Bot sends `/pause <name>` → API call.
2. API updates `trading.strategy_state.state` (`{"paused": true}`) and
   publishes on Redis channel `tea:control:<name>`.
3. `PaperDriver` subscribed to that channel flips
   `self._paused = True`; `_handle_tick` bumps a `paused_skip`
   counter and returns before risk/strategy evaluation.
4. On engine restart, `PaperDriver` rehydrates from
   `trading.strategy_state`, so the pause survives.

Strategy code is not touched. Invariant I.1 holds: the strategy does
not know about paper vs. paused vs. live.

## Consequences

- Container count rises from 7 to 9 on staging. Compose health
  gating adjusted.
- New ADR obligates Phase 6 to carry these two services into live
  (it is reasonable to scrape host metrics in live; the addition is
  not ephemeral).
- Telegram bot now has authenticated commands. Confirm-before-
  destructive-action remains a hard rule (see ADR 0005 on kill
  switch semantics); the FSM for `/killswitch` requires the exact
  phrase `sí lo entiendo`.
- Worker jobs run inside `tea-api`. At the current scale (< 5
  concurrent research backtests, each < 5 min), this does not need
  a dedicated container.

## Revisit

Revisit if:

- A research run exceeds 30 min regularly → shard workers into
  a dedicated container.
- Prometheus retention grows past 10 GB → add remote-write to a
  managed store.
- Bot needs multi-user (> 5 people) → introduce real RBAC beyond a
  CSV of user IDs.
