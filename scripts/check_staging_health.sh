#!/usr/bin/env bash
# Staging health gate. Checks:
#   1. tea-engine, tea-telegram-bot, tea-postgres, tea-redis are Up.
#   2. Heartbeat in Redis is fresh (< 30 s).
# Exits non-zero on any failure so `make deploy-staging` can trigger rollback.

set -u

cd "$(dirname "$0")/.."

err() { echo "[health] FAIL: $*" >&2; exit 1; }

for service in tea-postgres tea-redis tea-engine tea-telegram-bot; do
    status=$(docker compose ps --format '{{.Service}}:{{.Status}}' | grep "^${service}:" | cut -d: -f2)
    case "$status" in
        *Up*) ;;
        *) err "${service} not Up (status=${status:-missing})" ;;
    esac
done
echo "[health] containers Up"

# Heartbeat freshness check via redis-cli inside tea-redis.
raw=$(docker exec tea-redis redis-cli --no-auth-warning GET tea:engine:last_heartbeat 2>/dev/null)
if [ -z "$raw" ]; then
    err "no heartbeat in Redis — engine not started yet or crashed"
fi
age=$(echo "$raw" | python3 -c "
import json, sys, time
try:
    d = json.loads(sys.stdin.read())
    print(int(time.time() - float(d.get('ts', 0))))
except Exception as e:
    print(-1)
")
if [ "$age" -lt 0 ] || [ "$age" -gt 30 ]; then
    err "heartbeat stale (age=${age}s)"
fi
echo "[health] heartbeat fresh (age=${age}s)"
echo "[health] OK"
