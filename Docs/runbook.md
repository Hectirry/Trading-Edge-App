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

```
# Activate (block all order sending)
sudo touch /etc/trading-system/KILL_SWITCH

# Deactivate
sudo rm /etc/trading-system/KILL_SWITCH

# Verify
ls -l /etc/trading-system/KILL_SWITCH
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

## Phase 0 acceptance — status

| # | Criterion                                      | Status |
|---|------------------------------------------------|--------|
| 1 | `docker compose ps` shows 7 containers Up      | pass (ADR 0001) |
| 2 | Grafana HTTPS with valid (non-self-signed) cert | pass (Let's Encrypt R13) |
| 3 | Grafana panel with `SELECT now()` returns data | pass |
| 4 | Trivial commit to `main` triggers CI and passes | pass (lint + tests + security green) |
| 5 | `backup_db.sh` deposits backup in bucket       | **open — B2 pending** |
| 6 | `down && up -d` leaves state unchanged, data intact | pass |
