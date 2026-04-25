# Trading-Edge-App

Multi-broker trading system (Binance + Bybit + Polymarket) over NautilusTrader.
Research → paper → live pipeline.

See [Docs/Design.md](Docs/Design.md) for full design. Operate via [Docs/runbook.md](Docs/runbook.md).

## Phase

Phase 0 — Infrastructure base. Postgres + Redis + stubs under Docker Compose.
Grafana exposed via existing Traefik (openclaw) at `https://187-124-130-221.nip.io/grafana`.

## Quickstart (VPS)

```
cp .env.example .env                              # edit, OR symlink to /etc/trading-system/secrets.env on the VPS
docker compose up -d
docker compose ps                                 # 7 containers Up
```

## Layout

See design doc section I.4. Key dirs:

- `infra/` — postgres init, grafana provisioning, scripts
- `src/trading/` — package (phases 1+)
- `config/` — TOML envs/strategies/brokers (phases 1+)
- `tests/` — unit + integration
