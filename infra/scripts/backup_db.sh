#!/usr/bin/env bash
# Daily Postgres backup — pg_dump | gzip → local dir → (optional) S3/B2 upload.
# Cron: 0 4 * * *
# Env: TEA_PG_USER, TEA_PG_DB, TEA_BACKUP_LOCAL_DIR,
#      (optional) TEA_B2_ENDPOINT, TEA_B2_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
set -euo pipefail

SECRETS=/etc/trading-system/secrets.env
[[ -r "$SECRETS" ]] && set -a && . "$SECRETS" && set +a

: "${TEA_PG_USER:?TEA_PG_USER missing}"
: "${TEA_PG_DB:?TEA_PG_DB missing}"
: "${TEA_BACKUP_LOCAL_DIR:=/var/backups/tea}"

mkdir -p "$TEA_BACKUP_LOCAL_DIR"

ts=$(date -u +%Y%m%dT%H%M%SZ)
fname="tea-${TEA_PG_DB}-${ts}.sql.gz"
fpath="${TEA_BACKUP_LOCAL_DIR}/${fname}"

echo "[backup_db] dumping ${TEA_PG_DB} -> ${fpath}"
docker exec -i tea-postgres pg_dump -U "$TEA_PG_USER" -d "$TEA_PG_DB" | gzip -9 > "$fpath"

size=$(stat -c%s "$fpath")
echo "[backup_db] local ok: ${fpath} (${size} bytes)"

if [[ -n "${TEA_B2_ENDPOINT:-}" && -n "${TEA_B2_BUCKET:-}" ]]; then
    echo "[backup_db] uploading to s3://${TEA_B2_BUCKET}/daily/${fname}"
    AWS_ACCESS_KEY_ID="${TEA_B2_KEY_ID}" AWS_SECRET_ACCESS_KEY="${TEA_B2_APP_KEY}" \
        aws --endpoint-url "$TEA_B2_ENDPOINT" s3 cp "$fpath" "s3://${TEA_B2_BUCKET}/daily/${fname}"
    echo "[backup_db] upload ok"
else
    echo "[backup_db] B2 not configured — local-only (pending B2 account setup)"
fi

echo "[backup_db] pruning local >14 days"
find "$TEA_BACKUP_LOCAL_DIR" -type f -name "tea-*.sql.gz" -mtime +14 -delete
echo "[backup_db] done"
