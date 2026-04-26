# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Multi-broker trading system (Binance + Bybit + Polymarket) on a Python 3.11 stack. Research → paper → live pipeline, phase-driven per `Docs/Design.md`. The codebase is currently between Phase 3 (paper trading 24/7) and Phase 5/6 (LLM copilot, live capital). Live mode is wired but not enabled.

## Authoritative docs to read before non-trivial work

- `Docs/Design.md` — phase plan, FIRME/PROVISIONAL/ABIERTO decisions, invariants. Sections **I.1 (principios)** and **I.7 (guardrails)** are non-negotiable across all phases.
- `Docs/runbook.md` — deploy, ops, kill switch, per-phase acceptance status.
- `Docs/decisions/NNNN-*.md` — ADRs. Any decision affecting >1 strategy goes here, not in a strategy `.md`.
- `estrategias/README.md` — strategy lifecycle rules. Includes the **FAIL/STALE gate** at the start of each session (see below).
- Spanish is the primary doc language; code/comments are English.

## Non-negotiable invariants (Design.md I.1 / I.7)

1. **Same strategy code in backtest / paper / live.** No `if mode == "live"` branches. Mode differences come from config and adapters.
2. **Default mode = paper.** Live requires both `TRADING_ENV=production` AND `/etc/trading-system/I_UNDERSTAND_THIS_IS_REAL_MONEY` (enforced in `src/trading/engine/node.py`).
3. **Kill switch** — `/etc/trading-system/KILL_SWITCH` (host) OR `/var/tea/control/KILL_SWITCH` (API path, ADR 0009). Either path arms it. Backtest **ignores** the switch (ADR 0005); paper/live respect it.
4. **Idempotency** — `client_order_id` deterministic from `sha256(strategy|slug|ts|side)[:16]`. All ingest tables use `ON CONFLICT DO NOTHING`.
5. **Reproducibility** — pinned deps in `pyproject.toml`, fixed seeds, git commit hash in every backtest report.
6. **Research/production wall** — the LLM copilot has NO function calling, NO tool use; system prompt says so and any `tool_calls` in the response are stripped (ADR 0010). This is a design wall, not a flag.
7. **Strategy source files are NEVER shipped to LLM provider** (`llm_include_source=false`).

## Architecture (top-down)

### Process topology — 9 Docker containers

`tea-postgres` (TimescaleDB), `tea-redis`, `tea-ingestor`, `tea-engine`, `tea-api`, `tea-telegram-bot`, `tea-grafana`, `tea-prometheus`, `tea-node-exporter`. Reverse proxy / TLS is delegated to a pre-existing Traefik (ADR 0001 — Caddy was removed from the design).

The engine container runs **`paper_engine` by default** (not the backtest CLI). Backtests are invoked via `docker exec tea-engine python -m trading.cli.backtest …`.

### `src/trading/` package layout

- `common/` — `config.py` (pydantic-settings, `TEA_*` env prefix), shared types.
- `ingest/{binance,bybit,polymarket}/` — one adapter per broker. Each implements `backfill(symbol, from, to)`, `stream(symbols)`, `healthcheck()`. Polymarket uses Gamma API (metadata) + CLOB WebSocket (live book) + Data API (reconciliation) — Goldsky is NOT used (Design.md correction v1.1).
- `engine/`
  - `node.py` — `create_trading_node(mode, strategy_name)` factory. Backtest = working; paper = handle (real engine in `cli.paper_engine`); live = raises `NotImplementedError` (Phase 6).
  - `backtest_driver.py` — Phase 2 custom lightweight driver (ADR 0006). Nautilus is pinned in the `engine-live` extra but NOT installed in the image yet.
  - `strategy_base.py` — `StrategyBase` interface every strategy inherits. `ctx.recent_ticks` carries full per-market history (do not slice in the driver).
  - `fill_model.py` — parabolic fee model, replicated bit-exact from polybot-btc5m. Used in both backtest and paper.
  - `risk.py`, `sizing.py` — per-strategy `RiskManager`, Kelly-fractional sizing.
  - `features/` (micro, macro, mlofi, vpin, microprice, jumps) — every feature accepts an `as_of_ts` and must not peek forward; tests enforce this with synthetic ticks.
  - `walk_forward.py` — IS/OOS rolling driver (`research.walk_forward_runs` in DB).
- `paper/` — `driver.py` orchestrates one `PaperDriver` per enabled strategy; `feeds.py` (Binance kline_1s master clock + Polymarket Chainlink oracle + CLOB WS); `tick_recorder.py` writes to `market_data.paper_ticks` and publishes Redis `tea:paper_ticks`; `exec_client.SimulatedExecutionClient` checks kill switch, rejects on stale book / late entry, applies fee model, persists to `trading.orders`/`trading.fills`.
- `strategies/polymarket_btc5m/` — current strategy family. Live in paper: `last_90s_forecaster_v3` (active with declared gate-bypass) and `trend_confirm_t1_v1`. In-development: `bb_residual_ofi_v1`. Helpers prefixed with `_` (`_lgb_runner.py`, `_microstructure_provider.py`, `_v2_features.py`, etc.) are shared modules — `_v2_features.py` is the canonical 21-feature builder reused by v3 and stays even though `v2.py` is gone.
- `cli/` — entrypoints: `backfill`, `backtest`, `mc`, `paper_engine`, `walk_forward`, `daily_report`, `paper_vs_backtest`, `train_last90s`, `train_hmm_regime`, `train_bb_ofi`, `ingestor`.
- `api/` — FastAPI + Jinja2 + HTMX + Alpine.js dashboard at `/research`, JSON API at `/api/v1/*`. Auth via `X-TEA-Token` header or `tea_token` cookie. The `/api/v1/llm/*` endpoints have NO function calling (ADR 0010).
- `bots/telegram/` + `notifications/` — heartbeat watcher + interactive command poller. Cron jobs for daily report and Sunday paper-vs-backtest live in this container.
- `llm/` — OpenRouter integration; redaction hook in structlog; whitelist of allowed models.
- `research/` — `report.py` (HTML via Jinja2 + plotly), reports persisted in `tea_research_reports` named volume.

### Database

TimescaleDB. Three schemas:

- `market_data` — `crypto_ohlcv`, `crypto_trades` (90d retention), `polymarket_prices`, `polymarket_trades` (180d), `polymarket_markets`, `paper_ticks` (30d). Hypertables on `ts`.
- `trading` — `orders`, `fills`, `positions_snapshots`, `strategy_state` (paused state survives restart via Redis pub/sub on `tea:control:<strategy>`).
- `research` — `backtests`, `backtest_trades`, `walk_forward_runs`, `paper_vs_backtest_comparisons`, `models` (`is_active` flips only when AUC ≥ 0.55 / Brier ≤ 0.245 / ECE ≤ 0.05, ADR 0011), `strategy_health`, `llm_conversations`.

Init scripts: `infra/postgres/init/*.sql` (numbered `01…08`).

## Strategy contract — three parallel artifacts

For each strategy `<name>` (snake_case, ends in `_vN`):

```
estrategias/<estado>/<name>.md         ← hypothesis + history (en-desarrollo / activas / descartadas)
src/trading/strategies/<family>/<name>.py
config/strategies/<prefix>_<name>.toml   (sections: [params][sizing][backtest][fill_model][risk][paper])
```

Adding a strategy requires editing the dispatch in **both** `cli/backtest.py` AND `cli/paper_engine.py` (ADR 0008 — no dynamic discovery on purpose). The `tea-strategy-template` skill enumerates every file to touch and the canonical TOML layout.

State changes happen by **moving the `.md` file** between `en-desarrollo/`, `activas/`, `descartadas/`. Never delete a discarded strategy's `.md` — it's institutional learning.

`estrategias/INDICE.md` is the at-a-glance index (≤ 1 screen). Update it on every state change, at session **end** (not start).

## Session start ritual (MANDATORY when working on strategies)

Read `estrategias/resultados/_last_run_status.md` **first**. If `Status: FAIL` or `Timestamp` >36h old:

1. Stop. Do not read other files.
2. Show the user the raw status content (and `tail 20` of stderr if FAIL).
3. Wait for explicit confirmation ("seguir" / "ignorar" / "investigar"). Silence is not confirmation.

Only when status is `OK` and fresh, continue to `INDICE.md` and the specific `.md` files the user names.

`logs/vps-daily.log` is **not** read by default — only when the status `tail 20` is insufficient to diagnose a FAIL, and only the relevant timestamp block.

## Common commands

### Tests + lint

```bash
pytest -q                       # unit only (default per pyproject)
pytest -q -m integration        # hits real external APIs; off in CI
pytest tests/unit/strategies/test_imbalance_v3.py::test_name   # single test
ruff check .
ruff format .
pre-commit run --all-files      # ruff + gitleaks + yaml/EOF/whitespace
```

CI (`.github/workflows/`): `tests.yml` runs `pip install -e '.[dev,api]'` then `pytest -q`; `lint.yml` runs ruff; `security.yml` runs gitleaks.

### Backtest / walk-forward (run inside engine container)

```bash
docker exec tea-engine python -m trading.cli.backtest \
  --strategy polymarket_btc5m/<name> \
  --params config/strategies/<prefix>_<name>.toml \
  --from 2026-04-17T15:05:19Z --to 2026-04-21T23:59:59Z \
  --source polybot_sqlite \
  --polybot-db /polybot-btc5m-data/polybot.db
# Add --slug-encodes-open-ts when reading polybot-agent.db (BTC-Tendencia-5m).

docker exec tea-engine python -m trading.cli.walk_forward \
  --strategy polymarket_btc5m/<name> \
  --params config/strategies/<prefix>_<name>.toml \
  --from 2026-03-01 --to 2026-04-20 \
  --train-days 5 --test-days 1 --step-days 1
```

Defaults for walk-forward are 5d IS / 1d OOS / 1d step. Verdict ∈ {`promote`, `soak_longer`, `hold`, `insufficient_folds`}; `--promote-winner` is opt-in and gated by ADR 0011 thresholds.

### Monte Carlo evaluation

Two flavors, both keyed off a strategy + dataset window:

```bash
# Trade-vector bootstrap + permutation test (cheap):
docker exec tea-engine python -m trading.cli.mc \
  --strategy polymarket_btc5m/<name> \
  --params config/strategies/<prefix>_<name>.toml \
  --from 2026-04-01T00:00:00Z --to 2026-04-20T00:00:00Z \
  --source polybot_sqlite --polybot-db /polybot-btc5m-data/polybot.db \
  --kind bootstrap --n-iter 1000

# Block bootstrap (re-runs the driver against resampled markets — expensive):
#   add --kind block, or --kind both for both at once.
```

Persists to `research.mc_runs` (`kind ∈ {bootstrap, block}`). Bootstrap verdict ∈ {`edge_likely`, `no_edge`, `inconclusive`} based on p5 of total PnL and the coin-flip permutation p-value (`<0.05` → likely edge, `≥0.10` → no edge). Pure functions live in `src/trading/research/monte_carlo.py` and are reusable from notebooks. Distinct from `src/trading/engine/monte_carlo.py` which is a per-strategy bootstrap of spot prices for `mc_prob_up` (used inside `trend_confirm_t1_v1`) — do not merge them.

### Backfill

```bash
docker exec tea-ingestor python -m trading.cli.backfill \
  --broker {binance|bybit|polymarket} --symbol BTCUSDT \
  --interval 5m --from 2025-04-22T00:00:00Z --to 2026-04-22T00:00:00Z
```

The `tea-backfill-pattern` skill is the canonical template for new one-shot ingest scripts (rate limiting, browser UA for Cloudflare, `ON CONFLICT DO NOTHING`, resume semantics).

### Deploy / ops

```bash
make deploy-staging     # git pull --rebase + rebuild engine+telegram + health check; auto-rollback on fail
make rollback-staging   # git reset --hard HEAD~1 + rebuild
make check-staging      # ./scripts/check_staging_health.sh — engine up, heartbeat <30 s
make logs-engine        # docker compose logs -f --tail=200 tea-engine
make logs-telegram
make ps
```

The Makefile invokes destructive `git reset --hard HEAD~1` on rollback — flag this to the user before running.

### Promotion to active

Before flipping `research.models.is_active=true` on any strategy, use the `tea-promotion-gate` skill — it codifies ADR 0011 gates (AUC, Brier, ECE), walk-forward stability, and paper PnL signal.

## Things that look legit but aren't

- **`sharpe_annualized` from polybot-btc5m JSONs uses a hardcoded `trades_per_year=4000`** — economically meaningless. Use `sharpe_per_trade` for parity comparisons; report `sharpe_daily` as the trustworthy figure. Documented in `Docs/runbook.md` § Sharpe audit.
- **Polybot SQLite mounts at `/polybot-btc5m-data/` and `/btc-tendencia-data/` are FROZEN** (upstream bots stopped writing 2026-04-26 ~03:12 UTC). They remain readable but never grow. Consumers warn via `warn_if_polybot_stale` in `engine/data_loader.py`. Don't trust them as a current source.
- **Schema retention policies will drop data** — `crypto_trades` 90d, `polymarket_trades` 180d, `paper_ticks` 30d. Backfill scripts must respect these windows.
- **Phase 2 backtest driver** is a custom lightweight one, NOT NautilusTrader (ADR 0006). The `engine-live` extra ships Nautilus but it isn't installed in the image. Don't reach for `nautilus_trader` imports in Phase 2/3 code.
- **`TRADING_ENV` is duplicated by design** — `node.py` reads bare `TRADING_ENV` directly as a live-mode safety guard; pydantic Settings reads `TEA_TRADING_ENV`. Set both in `.env` to avoid silent fallback.

## Secrets

- File: `/etc/trading-system/secrets.env` (chmod 600, owner `coder:coder` so docker-compose running as `coder` can read it).
- `.env` in repo root is a symlink to that file. Recreate with `ln -sf /etc/trading-system/secrets.env /home/coder/Trading-Edge-App/.env` if missing.
- Never commit secrets. `.gitleaks.toml` + pre-commit + CI security workflow enforce.
- New secret? Generate with `openssl rand -hex 32`, append to the secrets file, then `docker compose up -d` to pick it up.

## Editing config TOMLs vs editing Python

Strategy parameter changes go in `config/strategies/<name>.toml`. The TOML's `[params]` is the source of truth. Don't introduce a parallel YAML/JSON config — TEA explicitly chose TOML and a single manifest per strategy.

`config/environments/{dev,staging,production}.toml` selects mode + which strategies are enabled (`[strategies.<name>] enabled = true`). Disable takes effect on next engine restart; there is no hotswap.
