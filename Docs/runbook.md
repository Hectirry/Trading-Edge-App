# Trading-Edge-App — Runbook

Phase 0 operational reference. Short, actionable. Keep updated per phase.

---

## Topology

- VPS: `srv1537368` (Lituania), `187.124.130.221`.
- Public hostname: `187-124-130-221.nip.io` (nip.io wildcard DNS; no domain).
- Stack: 7 containers under `docker-compose.yml` in `/home/coder/Trading-Edge-App/`.
  - `tea-postgres`, `tea-redis`, `tea-ingestor`, `tea-engine`, `tea-api`,
    `tea-telegram-bot`, `tea-grafana`.
  - `tea-caddy` intentionally omitted — see ADR 0001. Reverse proxy + TLS
    delegated to the pre-existing Traefik (`traefik-u6lx-traefik-1`).
- Secrets: `/etc/trading-system/secrets.env` (root-owned, `chmod 600`).
- Kill switch: `/etc/trading-system/KILL_SWITCH` (file absent by default).

## Acceptance criterion 1 (Phase 0)

7 containers `Up` (Caddy removed — reverse proxy delegated to existing Traefik).

---

## SSH to VPS

```
ssh coder@187.124.130.221
```

Root login disabled. Password auth disabled. Pubkey only.

---

## Container operations

List:
```
cd /home/coder/Trading-Edge-App && docker compose ps
```

Restart one service:
```
docker compose restart tea-postgres
```

Stop / start whole stack:
```
docker compose down
docker compose up -d
```

Idempotent — `up -d` twice leaves the same state. Postgres volume
(`tea_pgdata`) persists across `down`/`up`.

Rebuild (e.g. after editing postgres Dockerfile):
```
docker compose build tea-postgres && docker compose up -d tea-postgres
```

---

## Logs

Follow one service:
```
docker compose logs -f --tail=200 tea-grafana
```

All services:
```
docker compose logs -f --tail=50
```

Docker rotates logs (`10 MB x 5` per service).

---

## Postgres access

Shell:
```
docker exec -it tea-postgres psql -U "$TEA_PG_USER" -d "$TEA_PG_DB"
```

From host (port 5434, localhost only):
```
psql -h 127.0.0.1 -p 5434 -U tea trading_edge
```

Check extension + schemas:
```
\dn
SELECT extname, extversion FROM pg_extension;
```

---

## Backups

Location: `/var/backups/tea/` on VPS (local retention 14 days).
Remote: Backblaze B2 — **not yet configured** (pending account creation).

Manual backup:
```
bash /home/coder/Trading-Edge-App/infra/scripts/backup_db.sh
```

Cron (user `coder`, 04:00 UTC daily):
```
0 4 * * * /home/coder/Trading-Edge-App/infra/scripts/backup_db.sh >> /var/log/tea-backup.log 2>&1
```

Restore (destructive):
```
bash /home/coder/Trading-Edge-App/infra/scripts/restore_db.sh /var/backups/tea/tea-trading_edge-YYYYMMDDTHHMMSSZ.sql.gz
```

---

## Grafana

URL: `https://187-124-130-221.nip.io/grafana/`
Admin user/password: in `/etc/trading-system/secrets.env`
(`TEA_GF_ADMIN_USER`, `TEA_GF_ADMIN_PASSWORD`).

Datasource `TEA-Postgres` is pre-provisioned and read-only in the UI.
Dashboards ship from `infra/grafana/dashboards/` (`hello` shows
`SELECT now()` and TimescaleDB version).

---

## Kill switch

Convention (Design.md I.7): file exists = trading-engine refuses to
send orders. Default (file absent) = engine operates normally. Fail-safe
by design: accidental deletion merely stops blocking; it cannot
unblock something unsafe.

Phase 4 (ADR 0009) adds a second path writable from the API container.
Engine + exec client check BOTH paths with OR semantics; either
arms the switch.

```
# Operator (sudo-only):
sudo touch /etc/trading-system/KILL_SWITCH        # arm
sudo rm    /etc/trading-system/KILL_SWITCH        # disarm

# API / Telegram bot (no sudo, writes to the tea_control volume):
POST /api/v1/killswitch     { "confirm": "sí lo entiendo" }
POST /api/v1/killswitch_off
# touches / removes /var/tea/control/KILL_SWITCH

# Verify both paths:
ls -l /etc/trading-system/KILL_SWITCH /var/tea/control/KILL_SWITCH 2>/dev/null
```

Phase 0 has no trading-engine tick yet — convention and path are
established so that Phases 2+ can read the file each tick and at
startup without changes to infra.

---

## Health check (is the system OK?)

```
docker compose ps                                    # all 7 Up
docker compose exec tea-postgres pg_isready -U tea   # postgres ready
docker compose exec tea-redis redis-cli ping         # PONG
curl -sI https://187-124-130-221.nip.io/grafana/api/health | head -1
```

---

## Secrets

- File: `/etc/trading-system/secrets.env`
- Permissions: `chmod 600`, owner `root:root`.
- Never commit. `.gitignore` and gitleaks (pre-commit + CI) enforce.

Editing:
```
sudo -e /etc/trading-system/secrets.env
docker compose up -d                                 # pick up new env
```

Compose interpolation: the project dir has a symlink
`/home/coder/Trading-Edge-App/.env -> /etc/trading-system/secrets.env`.
Docker Compose reads it for variable substitution at config time. The
same file is also mounted into each container via `env_file`, so a
single source of truth governs both paths. If the symlink is deleted,
`${TEA_*}` interpolation breaks; recreate with:
```
ln -sf /etc/trading-system/secrets.env /home/coder/Trading-Edge-App/.env
```

---

## Gitleaks

Pre-commit runs gitleaks locally. CI (`.github/workflows/security.yml`)
runs it on every push/PR. If a false positive blocks a commit, allowlist
the path/regex in `.gitleaks.toml` — never commit `--no-verify`.

One-off full scan:
```
gitleaks detect --source . --verbose
```

---

## Phase 1 — Ingest

The `tea-ingestor` container runs the supervisor at
`src/trading/cli/ingestor.py` with five concurrent streams:
Binance OHLCV (5 intervals × 2 symbols), Binance trades (BTCUSDT),
Bybit OHLCV (same shape), Bybit trades (BTCUSDT), and the Polymarket
discovery + CLOB WebSocket loop. Metrics are exposed on
`tea-ingestor:9000/metrics` over `tea_internal` only (never bound to
the host).

Historical backfill CLI (one-shot, idempotent):
```
docker exec tea-ingestor python -m trading.cli.backfill \
  --broker binance --symbol BTCUSDT --interval 5m \
  --from 2025-04-22T00:00:00Z --to 2026-04-22T00:00:00Z
```

Valid brokers: `binance | bybit | polymarket`. For Polymarket, omit
`--symbol/--interval`; it uses `series_id=10684` and the slug prefix
is fixed.

Kill/restart of the ingestor is idempotent — PKs on every table reject
duplicates via `ON CONFLICT DO NOTHING`:
```
docker compose kill tea-ingestor && docker compose up -d tea-ingestor
```

Data retention policies (enforced by TimescaleDB):
- `market_data.crypto_trades`: 90 days
- `market_data.polymarket_trades`: 180 days
- OHLCV and `polymarket_prices`: no retention (cheap to keep)

Freshness dashboard: `https://187-124-130-221.nip.io/grafana/d/tea-data-freshness`.
Thresholds are relative (age divided by the candle period), so a 1d candle
with a 20 h age still shows green, and a 1m candle stuck for 3 min goes
red.

## Phase 1 acceptance — status

| # | Criterion                                                              | Status |
|---|------------------------------------------------------------------------|--------|
| 1 | Binance BTCUSDT 5m rows ≥ 100 000                                      | pass (105 125) |
| 2 | Polymarket `btc-updown-5m-%` markets ≥ 8 000                           | pass (8 999) |
| 3 | Live stream gap < interval period (or < 60 s for trades)               | pass |
| 4 | Kill + restart `tea-ingestor` → no duplicate `trade_id`                | pass (0 dupes) |
| 5 | `backfill` run twice on the same range → row count unchanged           | pass |
| 6 | Unit tests green in CI                                                 | pass |
| 7 | Grafana "Data freshness" dashboard shows all series green              | pass |

## Phase 2 — Backtest engine

The `tea-engine` container now runs a real image (`tea-engine:0.3.0`) with the
backtest driver, strategy port, and report generator. Phase 2 uses a
custom lightweight driver (see ADR 0006); Nautilus is pinned in
`pyproject.toml`'s `engine-live` extra but is NOT installed in the
Phase 2 image. Phase 3 will swap the driver for Nautilus's own event
loop once we need live execution plumbing.

### Backtest CLI

```
docker exec tea-engine python -m trading.cli.backtest \
  --strategy polymarket_btc5m/imbalance_v3 \
  --params config/strategies/pbt5m_imbalance_v3.toml \
  --from 2026-04-17T15:05:19Z --to 2026-04-21T23:59:59Z \
  --source polybot_sqlite \
  --polybot-db /polybot-btc5m-data/polybot.db
```

Writes an HTML report under `src/trading/research/reports/` (kept in
the `tea_research_reports` named volume) and inserts one row into
`research.backtests` + N rows into `research.backtest_trades`.

### Walk-forward CLI

```
docker exec tea-engine python -m trading.cli.walk_forward \
  --strategy polymarket_btc5m/imbalance_v3 \
  --params config/strategies/pbt5m_imbalance_v3.toml \
  --from 2026-04-17T15:05:19Z --to 2026-04-21T23:59:59Z \
  --train-days 2 --test-days 1 --step-days 1 \
  --out /tmp/wf.json
```

Persists to `research.walk_forward_runs`. Verdict is `stable` when
both total PnL and win-rate on every OOS split fall within ±30 % of
the cross-split mean; otherwise `unstable`.

### Kill switch semantics in backtest

Backtest mode ignores the `/etc/trading-system/KILL_SWITCH` file. The
switch only gates paper/live (Phases 3/6). This is documented in ADR
0005; the factory in `src/trading/engine/node.py` enforces it.

### Sharpe audit (legacy metric vs honest metric)

The polybot-btc5m JSONs report `sharpe_annualized` using a hardcoded
`trades_per_year=4000.0` (`core/kpis.py:198`). With
`sharpe_per_trade=0.696` that yields 44.04, which has no economic
meaning. Our reports show three Sharpe figures:

- `sharpe_per_trade` — raw mean/std of per-trade PnL. Our primary metric.
- `sharpe_annualized_iid` — `sharpe_per_trade * sqrt(trades_per_year_actual)`.
  Still inflated because trades cluster intraday and are not i.i.d.
  We report it with a warning banner.
- `sharpe_daily` — mean/std of daily-aggregated PnL × √365. Most
  trustworthy given the clustering.

When comparing a Phase 2 backtest against a polybot JSON, use
`sharpe_per_trade` (bit-exact match) and treat annualized numbers as
indicative only.

### Parity test

Read-only mount at `/polybot-btc5m-data/polybot.db` plus
`/polybot-btc5m-reports/*.json` supports the parity probe. Bit-exact
parity achieved against `backtest_imbalance_v3_20260422_134025.json`
(305/305 trades, 0 price drift, 0 PnL drift).

```
docker exec tea-engine python scripts/parity_probe.py \
  /polybot-btc5m-data/polybot.db \
  /polybot-btc5m-reports/backtest_imbalance_v3_20260422_134025.json
```

### Grafana Backtests dashboard

`https://187-124-130-221.nip.io/grafana/d/tea-backtests` — lists the
most recent backtests, latest equity curve, and walk-forward runs.

## Phase 2 acceptance — status

| # | Criterion                                                               | Status |
|---|-------------------------------------------------------------------------|--------|
| 1 | Sharpe audit delivered before migration code                            | pass (see section above) |
| 2 | CLI runs without error, writes HTML + `research.backtests` row          | pass |
| 3 | Parity: 0 differences vs polybot JSON trade vector                      | pass (305/305, 0 drift) |
| 4 | HTML report opens in the browser                                        | pass (plotly + Jinja render verified) |
| 5 | Walk-forward produces a verdict with numeric justification              | pass (unstable on tiny 4.4d sample, documented) |
| 6 | Unit tests pass in CI                                                   | pass (54 tests) |
| 7 | 6-month × 5-min backtest completes under 5 minutes on the VPS           | pass by extrapolation (4.4d × 1s runs in ~3s) |

## Phase 3 — Paper trading 24/7

The `tea-engine` image (`tea-engine:0.3.0`) now runs the paper engine by
default (`python -m trading.cli.paper_engine`). It subscribes to three
live feeds, composes ticks, persists them to `market_data.paper_ticks`,
and calls the same `imbalance_v3` strategy code that the backtest uses.

### Deploy

```
make deploy-staging   # git pull + rebuild + restart + health check
make rollback-staging # revert last commit and redeploy
make check-staging    # run health probe only
make logs-engine
make logs-telegram
```

The health check asserts that every service is Up and that the Redis
heartbeat is < 30 s old. A failing check inside `deploy-staging` triggers
an automatic `rollback-staging`.

### Live feeds

- `wss://stream.binance.com:9443/ws/btcusdt@kline_1s` — master clock at 1 Hz.
- `wss://ws-live-data.polymarket.com` — Chainlink BTC/USD oracle (settles windows).
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` — CLOB book per token.

All three reconnect with exponential backoff (1 → 60 s) on failure.
The CLOB frame limit is lifted to 16 MiB because subscribing to ~200
tokens (100 open markets × YES/NO) sends a large initial payload.

### paper_ticks

Every 1 s the tick recorder composes one row per open market and writes
to `market_data.paper_ticks`. Retention is 30 days (Timescale policy).
Each row also publishes on Redis channel `tea:paper_ticks` for the
paper driver to consume synchronously.

### Paper driver + SimulatedExecutionClient

The driver receives ticks from Redis, maintains one `IndicatorStack`
per market, and calls the same `imbalance_v3` code path that the
Phase 2 backtest uses. Orders go through
`trading.paper.exec_client.SimulatedExecutionClient` which:

- Checks `KILL_SWITCH` on every submit (alert-only per ADR 0005).
- Rejects on stale book (> 10 s without a CLOB update).
- Rejects on late entry (`t_in_window > latest - 5 s`).
- Uses the parabolic fee model from `engine/fill_model.py`.
- Persists orders + fills to `trading.orders` / `trading.fills` with
  `mode='paper'`, `client_order_id` deterministic from
  `sha256(strategy|slug|ts|side)[:16]`, so restarts do not duplicate.

### Heartbeat + alerts

The engine publishes a JSON heartbeat to Redis key
`tea:engine:last_heartbeat` every 10 s (TTL 120 s). The watcher in
`tea-telegram-bot` polls every 30 s and fires Telegram alerts on
`HEARTBEAT_LOST` / `HEARTBEAT_RECOVERED` transitions.

Alert severities: INFO (trade events, heartbeat recovered), WARN (loss
threshold, kill-switch removed), CRIT (engine stopped, heartbeat lost,
reconciliation fail, kill-switch active). Each kind has a 60 s dedupe
window. A circuit breaker suppresses alerts for 15 min after 20 sends
in 60 s.

### Reconciliation — alert only

Every 5 min the driver compares its in-memory ledger against
`trading.fills`. Divergence fires a CRIT alert and logs detail; it does
NOT auto-pause. Manual halt options: `sudo touch
/etc/trading-system/KILL_SWITCH` (blocks new orders) or docker-restart
the engine.

### Daily report

Cron inside `tea-telegram-bot`: fires at 00:05 UTC every day.
`python -m trading.cli.daily_report` aggregates yesterday's fills and
posts to Telegram. Force a report for a specific day:

```
docker exec tea-telegram-bot python -m trading.cli.daily_report \
  --date 2026-04-22 --print-only
```

### Weekly paper-vs-backtest

Cron inside `tea-telegram-bot`: fires Sundays at 01:00 UTC.
Replays the backtest driver against `market_data.paper_ticks` for the
previous 7 days, compares against `trading.orders`/`trading.fills`
with `mode='paper'`, persists to `research.paper_vs_backtest_comparisons`
and posts a Telegram summary.

Verdict thresholds: `divergent` when `|Δtrades| > 10 %` or
`|Δpnl| > 20 %`; else `aligned`.

### Kill switch operational notes

Phase 3 enforces the kill switch only on paper and live:

```
# Activate — engine stops accepting new orders; open positions still settle.
sudo touch /etc/trading-system/KILL_SWITCH

# Deactivate
sudo rm /etc/trading-system/KILL_SWITCH
```

An edge-triggered Telegram alert (`KILL_SWITCH_ON` / `KILL_SWITCH_OFF`)
fires on each transition. Backtest mode ignores the file (ADR 0005).

### Phase 3 acceptance checklist

Track against the Phase 3 criteria once 7 days of paper uptime pass:

- [ ] `tea-engine` up 7 days with no manual restart.
- [ ] ≥ 50 paper trades in the same window.
- [ ] Weekly paper-vs-backtest divergence < 10 % trades / < 20 % PnL.
- [ ] Telegram alerts verified by killing `tea-engine` and confirming
      `HEARTBEAT_LOST` then `HEARTBEAT_RECOVERED` land in the channel.
- [ ] Kill switch tested end-to-end (`sudo touch` → order reject alert
      → `sudo rm` → recovery alert).
- [ ] Runbook reviewed after the first weekly comparison.

## Phase 3.5 — Second strategy: trend_confirm_t1_v1

Ported from `/home/coder/BTC-Tendencia-5m/strategies/trend_confirm_t1_v1.py`.
T-90 s horizon entry with 3-of-4 AFML confirmation + Chainlink adverse
gate. Runs alongside `imbalance_v3` in the same `tea-engine` under the
multi-strategy registry (ADR 0008). Each strategy owns its `RiskManager`,
stake, and capital.

### Config registry

`config/environments/staging.toml` enables both strategies:

```toml
[strategies.imbalance_v3]
enabled = true
params_file = "config/strategies/pbt5m_imbalance_v3.toml"

[strategies.trend_confirm_t1_v1]
enabled = true
params_file = "config/strategies/pbt5m_trend_confirm_t1_v1.toml"
```

Disable by setting `enabled = false`; takes effect on next engine
restart (no hotswap).

### Per-strategy capital + thresholds

Each strategy's TOML owns a `[paper]` section:

```toml
[paper]
capital_usd = 1000.0
daily_loss_alert_pct = 0.03
daily_loss_pause_pct = 0.05
```

`staging.toml` keeps the same three fields as a fallback only used
when a strategy doesn't specify its own `[paper]` block.

### Backtest + walk-forward with BTC-Tendencia-5m

BTC-Tendencia embeds `open_ts` in the slug (`btc-updown-5m-{open_ts}`)
instead of `close_ts`. Pass `--slug-encodes-open-ts` to backtest /
walk-forward CLIs when pointing at `polybot-agent.db`:

```
docker exec tea-engine python -m trading.cli.backtest \
  --strategy polymarket_btc5m/trend_confirm_t1_v1 \
  --params config/strategies/pbt5m_trend_confirm_t1_v1.toml \
  --from 2026-04-22T08:00:00Z --to 2026-04-22T20:15:00Z \
  --source polybot_sqlite \
  --polybot-db /btc-tendencia-data/polybot-agent.db \
  --slug-encodes-open-ts
```

### Parity (Phase 3.5 — bit-exact achieved)

`trend_confirm_t1_v1` matches polybot-agent's **backtest trade vector**
**141/141**, 0 price drift, 0 pnl drift. Reference extracted via
`/tmp/extract_polybot_agent_trades.py` (read-only invocation of
polybot-agent's own `core/backtest_engine.run_single_backtest`).
Authoritative parity probe: `scripts/parity_probe_trend_bt.py`.

Three strategy-level flags made this possible (see ADR 0008 addendum):

- `[risk].bypass_in_backtest = true` — polybot-agent's backtest skips
  its RiskManager; TEA matches.
- `[fill_model].apply_fee_in_backtest = true` + `fee_k = 0.05` —
  polybot-agent subtracts the parabolic fee in backtest PnL.
- `[fill_model].fill_probability = 1.0` — polybot-agent always fills
  in backtest (the 0.95 sim is PaperExecutor-only).

One backtest_driver change: `ctx.recent_ticks` now carries the full
per-market history (previously sliced to `[-30:]`). Required for
`trend_confirm_t1_v1`'s AFML features (`frac_size=60`,
`cusum_lookback=120`); backward-compatible with `imbalance_v3`, which
applies its own `[-30:]` slice inline for the depth-trend check.

The old `scripts/parity_probe_trend.py` compared against LIVE trades;
that probe is kept as a **correlation check** (not a parity gate),
because live ordering + evolving stake/config make a bit-exact match
impossible. Its tolerances are documented in the script and it is not
part of CI.

`imbalance_v3` stays at 305/305 against its polybot-btc5m JSON; no
regression from the Phase 3.5 changes.

Walk-forward on 4 d IS / 1 d OOS over the 6-day polybot-agent window
(2 splits): `unstable` (fold 0 +$24.54 WR 61.7 % vs fold 1 -$13.95
WR 64.5 %). Expected given tiny sample; revisit after 4 weeks of
TEA paper. Verdict tagged `inconclusive_small_sample` if any split
has n_trades_oos < 20.

### Grafana extension

`tea-paper-live` dashboard now has two extra panels:

- "PnL today per strategy" — trades and realized PnL grouped by
  `strategy_id` for the current UTC day.
- "Cumulative PnL per strategy (24h)" — time series split by
  `strategy_id` over the last 24 h.

## Phase 4 — Dashboard, Prometheus, interactive Telegram

See ADR 0009 for rationale. Nine containers now run (9 vs the Phase 0
seven); two new services and one heavy upgrade.

### Services added / upgraded

- **tea-api** (`tea-api:0.4.0`) — FastAPI + Jinja2 + HTMX + Alpine.js
  dashboard at `https://187-124-130-221.nip.io/research`. JSON API at
  `/api/v1/*`. Auth: `X-TEA-Token` header or `tea_token` cookie.
- **tea-prometheus** (`prom/prometheus:v3.1.0`) — scrapes
  `tea-ingestor:9000/metrics` and host `node_exporter` at
  `host.docker.internal:9100`. 30-day retention, volume
  `tea_prom_data`. Revokes the Phase 0 "no Prom" prohibition.
- **tea-node-exporter** (`prom/node-exporter:v1.8.2`) — host-mode
  container for CPU/RAM/disk/net metrics. Listens on
  `127.0.0.1:9100`; never reachable from off-host.
- **tea-telegram-bot** — now runs both the heartbeat/cron watcher AND
  the interactive command poller (9 commands).

### Secrets added

```
TEA_API_TOKEN=<openssl rand -hex 32>   # dashboard + bot→api auth
TEA_TELEGRAM_AUTHORIZED_USERS=<csv>    # e.g. 6160639204
```

Generated once via `openssl rand -hex 32`; written to
`/etc/trading-system/secrets.env` (chmod 600, owned by `coder`).
Token is shown on stdout once at creation; recover via
`sudo grep TEA_API_TOKEN /etc/trading-system/secrets.env`.

### Dual-path KILL_SWITCH

Two files are checked (OR) by engine + paper exec client:

```
/etc/trading-system/KILL_SWITCH       # host-managed, sudo-only
/var/tea/control/KILL_SWITCH          # tea_control volume, writable by tea-api
```

Operator flow: ssh + `sudo touch /etc/trading-system/KILL_SWITCH`.
Bot flow: `/killswitch` → confirm `sí lo entiendo` → API writes the
`/var/tea/control` file. Either path triggers the engine to refuse
new orders. Remove the file that was touched to clear.

### Pause / resume (Redis pub/sub)

```
POST /api/v1/strategies/<name>/pause    # sets trading.strategy_state.state.paused=true
                                        # + publishes on tea:control:<name>
```

`PaperDriver` subscribes to `tea:control:<strategy_name>`. On boot,
it rehydrates from `trading.strategy_state`, so a pause set via the
bot survives a container restart. Paused ticks bump the
`paused_skip` counter visible in the 60 s `eval_summary` log line.
Strategy code is not invoked while paused (Invariant I.1 holds).

### Grafana — new dashboards

Auto-provisioned on startup:

- `TEA — System health` — CPU / RAM / disk / load / network / ingestor
  up, all from Prometheus.
- `TEA — Market explorer` — upcoming BTC up/down 5m markets, live YES
  / NO prices for the next-closing market, tick freshness.
- `TEA — Strategy comparator` — latest completed backtest per
  strategy, backtest PnL history, paper cumulative PnL by strategy,
  paper-vs-backtest weekly verdicts.

A second Prometheus datasource (uid `tea-prometheus`) is provisioned
alongside the existing Postgres one.

### Dashboard usage

1. Browse to `https://187-124-130-221.nip.io/research` (redirects to
   `/login` on first visit).
2. Paste `TEA_API_TOKEN` — sets a secure `tea_token` cookie for 7 days.
3. Index lists completed / running / failed / queued backtests with
   filters by strategy + status. Checkboxes pick 2–3 rows to compare.
4. `new` launches a backtest worker (subprocess, 15-min timeout,
   stdout / stderr tails captured). `research/jobs/<id>` polls every
   2 s until the run completes and auto-links to the report.

### Telegram interactive commands

Only users listed in `TEA_TELEGRAM_AUTHORIZED_USERS` can invoke
commands. All others get `⛔ not authorized` + a log line.

| Command                | Effect |
|------------------------|--------|
| `/status`              | heartbeat age, kill-switch state, open positions, pnl today |
| `/positions`           | open paper positions list |
| `/trades [N]`          | last N (default 10) paper trades |
| `/pnl [hours]`         | pnl over last H hours (default 24) |
| `/pause <strategy>`    | pause via API (publishes on Redis) |
| `/resume <strategy>`   | resume via API |
| `/killswitch`          | 2-step; requires exact phrase `sí lo entiendo` within 120 s |
| `/backtest ...`        | queues async job, DMs a report link on completion |
| `/help`                | command reference |

## Phase 5 — LLM research copilot (OpenRouter)

See ADR 0010. Read-only research surface behind the Phase 4 auth.

### Services affected

- `tea-api` bumped to `tea-api:0.5.0` (`/api/v1/llm/chat`,
  `/api/v1/llm/reset`, `/research/chat`).
- `tea-telegram-bot` bumped to `tea-telegram-bot:0.4.0` (`/ask`,
  `/ask_reset`).
- `tea-postgres` gains two tables via `07_llm_conversations.sql`.
- `tea-grafana` auto-loads a new dashboard `TEA — LLM usage`.

### Secrets

Put the OpenRouter key into `/etc/trading-system/secrets.env`:

```
OPENROUTER_API_KEY=sk-or-...
```

`chmod 600` already enforced. It is read by `tea-api` and
`tea-telegram-bot` only. Never commit; ADR 0010 documents the
redaction hook in structlog.

### Apply

```bash
# Apply the new schema (idempotent) if the Postgres container has
# already started from a previous init-scripts run:
docker compose exec -T tea-postgres \
  psql -U "$TEA_PG_USER" -d "$TEA_PG_DB" \
  < infra/postgres/init/07_llm_conversations.sql

# Rebuild API + bot with the new code:
docker compose up -d --build tea-api tea-telegram-bot

# Poke Grafana to reload provisioning (or restart):
docker compose restart tea-grafana
```

### Defaults + caps

- default model: `qwen/qwen3-max` (0.78 in / 3.90 out per M tok)
- whitelist (all else rejected at the endpoint):
  `qwen/qwen3-max`,
  `anthropic/claude-sonnet-4.6`,
  `anthropic/claude-opus-4.6`,
  `openai/gpt-4o-mini`,
  `meta-llama/llama-3.3-70b-instruct`
- 50 sessions/user/day, 200 000 tok/session, $10/user/day cap
- `llm_include_source=false` — strategy source files are NEVER
  shipped to the provider by default; only TOML + metadata

### Hard wall (ADR 0010)

- `tools`, `tool_choice`, `function_call`, `functions` stripped from
  every outbound body; the system prompt says the model has none.
- Responses containing `tool_calls` / `function_call` raise
  `LLMPolicyError` and are discarded before persistence.
- All loaded context wraps in `<context type="…" id="…">…</context>`
  with a prompt that flags it as DATA, never instructions.
- User / session IDs scoped: `web:<sha256[:8](token)>` or
  `telegram:<user_id>`.

### /ask via Telegram

```
/ask <pregunta>      # creates or continues a session (Redis, 4h TTL)
/ask_reset           # clears session in Redis + deletes DB row
```

The bot does not attach context refs. Use `/research/chat` in the
dashboard if you want to pin backtests / ADRs to the prompt.

## Phase 3.6 — last_90s_forecaster v1 + v2

See ADR 0011. Two new paper strategies that enter at t≈210 s using
micro momentum (last 90 s of BTC) + macro regime (EMA 8/34 + ADX) +
Polymarket microstructure. v1 is rules-based; v2 is a LightGBM on
top of the same features.

### Services affected

- `tea-engine` bumped to `tea-engine:0.4.0` (adds `lightgbm==4.5.0`,
  `optuna==4.1.0`, `scikit-learn==1.6.0`, `psycopg2-binary`).
- `tea-postgres` gains two tables via `08_models_and_health.sql`
  (`research.models`, `research.strategy_health`).
- `tea-grafana` auto-loads `TEA — Strategy comparator 4-way`.

### Apply

```bash
# Schema (idempotent)
docker compose exec -T tea-postgres \
  psql -U "$TEA_PG_USER" -d "$TEA_PG_DB" \
  < infra/postgres/init/08_models_and_health.sql

# Rebuild engine with ML deps
docker compose up -d --build tea-engine
docker compose restart tea-grafana
```

### Train the v2 model

Mount the two polybot SQLite files (already declared in
docker-compose.yml as read-only volumes on tea-engine):

```bash
docker compose exec tea-engine python -m trading.cli.train_last90s \
  --from 2026-01-15 --to 2026-04-20 \
  --polybot-btc5m /polybot-btc5m-data/polybot_agent.db \
  --polybot-agent /btc-tendencia-data/polybot_agent.db \
  --optuna-trials 200 --time-budget-s 3600 \
  --promote
```

`--promote` only flips `is_active = TRUE` when all three gates pass:
`AUC_test ≥ 0.55 AND Brier_test ≤ 0.245 AND ECE_val ≤ 0.05`. Otherwise
the row is written with `is_active = FALSE` and the strategy stays in
shadow mode (SKIPs every ENTER, logs features + probs for analysis).

### Defaults + invariants

- Entry window: `t_in_window ∈ [205, 215]`. Outside → SKIP
  `outside_entry_window`.
- v1 `momentum_divisor_bps = 40` (provisional; grid-search before first
  heavy deploy — see ADR 0011).
- Both strategies start with `$5` fixed stake until they log 20 settled
  paper trades, then switch to Kelly-fractional (¼) capped at `$15`.
- Strategy source code is NEVER shipped to the LLM provider
  (`llm_include_source = false`). Orthogonal to the feature work; just
  a reminder that ADR 0010 still holds.

### Hard wall

- v2 has no tools / no function calls. It runs a frozen
  `lightgbm.Booster` against a deterministic feature vector and returns
  a single float.
- Retraining is manual — no auto-retrain path in v2.

## Known caveats / Phase 0 pending items

- B2 bucket not yet configured. `backup_db.sh` stores locally; remote
  upload is a no-op until `TEA_B2_*` env vars are set. Acceptance
  criterion 5 ("backup deposita en bucket") remains **open** until
  then.
- Grafana relies on external Traefik (openclaw) for TLS. If that
  Traefik is removed or reconfigured, Grafana loses HTTPS ingress.
- RAM on VPS is tight (7.8 GiB total). A 2 GiB swapfile mitigates. If
  future phases add ingestor + engine + Nautilus, re-evaluate capacity.
- Secrets file owner is `coder:coder` (not `root:root`) so Docker
  Compose, running as `coder`, can read it. Permissions remain `600`,
  so no other local user can read. Docker daemon already runs as
  root-equivalent for any member of the `docker` group, so this does
  not change the effective blast radius.

## Phase 3.8 — Walk-forward automation

Unified CLI `trading.cli.walk_forward`. Two execution paths inside:

- ML (`hmm_regime_btc5m`, `last_90s_forecaster_v3`,
  `bb_residual_ofi_v1`) — refit the model on each IS window and
  evaluate AUC/Brier on OOS.
- Rules (`trend_confirm_t1_v1`) — replay via the Phase-2
  `run_walk_forward` infrastructure over polybot SQLite, measuring
  trade count + PnL per fold.

Defaults: 5-day IS / 1-day OOS / step 1-day (approved for 3.8).
Results land in `research.walk_forward_runs` (per-fold detail in
`splits` JSONB; aggregate in `summary`). `--promote-winner` is
opt-in — promotion stays manual by default; summary includes a
`promote_recommendation` ∈ {promote, soak_longer, hold,
insufficient_folds} the operator can act on.

### Manual runs

```bash
# ML — rolling retrain
docker compose exec tea-engine python -m trading.cli.walk_forward \
    --strategy last_90s_forecaster_v3 \
    --from 2026-03-01 --to 2026-04-20

# Rules — replay
docker compose exec tea-engine python -m trading.cli.walk_forward \
    --strategy trend_confirm_t1_v1 \
    --params config/strategies/pbt5m_trend_confirm_t1_v1.toml \
    --from 2026-01-01 --to 2026-04-20 \
    --polybot-db /btc-tendencia-data/polybot-agent.db \
    --slug-encodes-open-ts
```

### Scheduled cron

The Telegram watcher's `run_walk_forward_sunday()` coroutine fires
every Sunday at 02:00 UTC, one strategy per minute (7 strategies
staggered over 7 minutes) using a trailing 30-day window.

### Dashboard

`TEA — Walk-forward` (Grafana, uid `tea-walk-forward`): table of
runs, median AUC_oos timeline by strategy, stability index timeline,
and per-fold detail for the most recent run of each strategy.

## Phase 3.9 — Monte Carlo evaluation

Two MC modes for completed backtests, persisted to `research.mc_runs`
(see `infra/postgres/init/12_mc_runs.sql`):

- **bootstrap** — resamples the realized trade vector with replacement.
  Reports realized + percentile distribution for total PnL, win rate,
  per-trade Sharpe, and max DD. Includes a coin-flip permutation
  p-value over wins/losses. Cheap (~seconds for 1k iter).
- **block** — re-runs `run_backtest` against bootstrap-resampled 5-min
  Polymarket markets. Strategy state is reset per replicate via the
  factory closure; heavy artifacts (LightGBM runners, macro provider)
  load once. Cost is `n_iter × backtest_runtime`.

```bash
# Both flavors at once (default --kind=both):
docker compose exec tea-engine python -m trading.cli.mc \
  --strategy polymarket_btc5m/last_90s_forecaster_v3 \
  --params config/strategies/pbt5m_last_90s_forecaster_v3.toml \
  --from 2026-04-01T00:00:00Z --to 2026-04-20T00:00:00Z \
  --source polybot_sqlite --polybot-db /polybot-btc5m-data/polybot.db \
  --kind both --n-iter 1000

# Smoke test without DB writes:
docker compose exec tea-engine python -m trading.cli.mc \
  ... --kind bootstrap --n-iter 200 --no-persist
```

Bootstrap verdict (column `verdict` on `kind='bootstrap'` rows) ∈
{`edge_likely`, `no_edge`, `inconclusive`} per the heuristic in
`research/monte_carlo.py:verdict_from_bootstrap`:

- `edge_likely` — p5 of total PnL > 0 **AND** permutation p-value < 0.05.
- `no_edge`     — p5 of total PnL ≤ 0 **OR** permutation p-value ≥ 0.10.
- `inconclusive` — anything in between.

The verdict is informative, not a promotion gate. Promotion still
requires the AUC/Brier/ECE thresholds in ADR 0011 and the walk-forward
checks. MC adds context on how tight the backtest result was; it does
not rescue a strategy that failed walk-forward.

`engine/monte_carlo.py` is **unrelated** — that's a per-strategy
spot-price bootstrap for the `mc_prob_up` confirmation gate used inside
`trend_confirm_t1_v1`. Do not merge.

## Phase 0 acceptance — status

| # | Criterion                                      | Status |
|---|------------------------------------------------|--------|
| 1 | `docker compose ps` shows 7 containers Up      | pass (ADR 0001) |
| 2 | Grafana HTTPS with valid (non-self-signed) cert | pass (Let's Encrypt R13) |
| 3 | Grafana panel with `SELECT now()` returns data | pass |
| 4 | Trivial commit to `main` triggers CI and passes | pass (lint + tests + security green) |
| 5 | `backup_db.sh` deposits backup in bucket       | **open — B2 pending** |
| 6 | `down && up -d` leaves state unchanged, data intact | pass |
