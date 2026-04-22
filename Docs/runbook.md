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

## Known caveats / Phase 0 pending items

- B2 bucket not yet configured. `backup_db.sh` stores locally; remote
  upload is a no-op until `TEA_B2_*` env vars are set.
- Grafana relies on external Traefik (openclaw) for TLS. If that
  Traefik is removed or reconfigured, Grafana loses HTTPS ingress.
- RAM on VPS is tight (7.8 GiB total). A 2 GiB swapfile mitigates. If
  future phases add ingestor + engine + Nautilus, re-evaluate capacity.
