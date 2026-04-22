#!/usr/bin/env bash
# Restore a gzipped pg_dump into tea-postgres. DESTRUCTIVE.
# Usage: infra/scripts/restore_db.sh <backup.sql.gz>
set -euo pipefail

SECRETS=/etc/trading-system/secrets.env
[[ -r "$SECRETS" ]] && set -a && . "$SECRETS" && set +a

: "${TEA_PG_USER:?}"
: "${TEA_PG_DB:?}"

FILE="${1:?usage: restore_db.sh <path.sql.gz>}"
[[ -r "$FILE" ]] || { echo "not readable: $FILE" >&2; exit 2; }

read -r -p "Restore ${FILE} into DB ${TEA_PG_DB}? Overwrites all data. Type 'yes': " ans
[[ "$ans" == "yes" ]] || { echo "aborted"; exit 1; }

gunzip -c "$FILE" | docker exec -i tea-postgres psql -U "$TEA_PG_USER" -d "$TEA_PG_DB"
echo "[restore_db] done"
