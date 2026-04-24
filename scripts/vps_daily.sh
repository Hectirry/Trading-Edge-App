#!/usr/bin/env bash
# VPS daily research runner.
#
# Responsibilities:
#   1. Pull main with rebase so we sit on top of any human work.
#   2. Run the configured list of backtests via trading.cli.backtest.
#   3. For each one, export a compact markdown summary into
#      estrategias/resultados/<name>/backtest-YYYY-MM-DD.md.
#   4. Write estrategias/resultados/_last_run_status.md with OK/FAIL,
#      timestamp, and last 20 lines of stderr on failure (Claude reads
#      this first each morning).
#   5. Commit only under estrategias/resultados/** and push to main,
#      retrying rebase-then-push with exponential backoff on conflict.
#
# Invoked by cron or systemd timer. See Docs/runbook.md.
#
# Requirements on VPS:
#   - /etc/trading-system/secrets.env has DATABASE_URL.
#   - SSH deploy key with WRITE permission configured as git origin.
#   - `git config user.email "vps-bot@trading-edge-app"` and user.name set.
#   - venv with project installed (pip install -e '.[dev,api]').

set -uo pipefail

REPO="${REPO_DIR:-/home/coder/Trading-Edge-App}"
VENV="${VENV_DIR:-$REPO/.venv}"
SECRETS="${SECRETS_FILE:-/etc/trading-system/secrets.env}"
BRANCH="${BRANCH:-main}"
MAX_PUSH_RETRIES=5

# Space-separated list: "<strategy>|<params.toml>|<from>|<to>|<source>"
BACKTESTS_FILE="${BACKTESTS_FILE:-$REPO/scripts/vps_daily.backtests}"

STATUS_FILE="$REPO/estrategias/resultados/_last_run_status.md"
STDERR_LOG="$(mktemp -t tea-vps-daily-stderr.XXXXXX)"

# Mirror stderr to a temp file so we can tail it into the status report,
# while still letting cron capture it in /var/log/tea-vps-daily.log.
exec 2> >(tee -a "$STDERR_LOG" >&2)

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cleanup() { rm -f "$STDERR_LOG"; }
trap cleanup EXIT

cd "$REPO"

# Load secrets so cron has DATABASE_URL etc. set -a exports every var.
if [ -f "$SECRETS" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS"
  set +a
else
  log "WARN: $SECRETS not found — DATABASE_URL likely missing"
fi

# Activate venv.
# shellcheck disable=SC1091
source "$VENV/bin/activate"

write_status() {
  local status="$1"
  local note="$2"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$(dirname "$STATUS_FILE")"
  {
    echo "# último run VPS"
    echo ""
    echo "Status: **$status**"
    echo "Timestamp: $ts"
    echo "Nota: $note"
    echo ""
    if [ "$status" != "OK" ]; then
      echo "## stderr (últimas 20 líneas)"
      echo ""
      echo '```'
      tail -n 20 "$STDERR_LOG" 2>/dev/null || echo "(no stderr capturado)"
      echo '```'
    fi
  } > "$STATUS_FILE"
}

fail() {
  local msg="$1"
  log "FAIL: $msg"
  write_status "FAIL" "$msg"
  commit_and_push_status_only || true
  exit 1
}

commit_and_push_status_only() {
  git add "$STATUS_FILE" || return 1
  if git diff --cached --quiet; then
    return 0
  fi
  git commit -m "chore(research): status update $(date -u +%Y-%m-%dT%H:%MZ)" || return 1
  push_with_backoff
}

push_with_backoff() {
  local attempt=0
  local delay=1
  until git push origin "$BRANCH"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$MAX_PUSH_RETRIES" ]; then
      log "push failed after $attempt attempts; leaving commit local"
      return 1
    fi
    log "push rejected, sleeping ${delay}s before rebase+retry (attempt $attempt/$MAX_PUSH_RETRIES)"
    sleep "$delay"
    delay=$((delay * 2))
    git fetch origin "$BRANCH" || return 1
    if ! git pull --rebase origin "$BRANCH"; then
      log "unexpected rebase conflict — aborting, investigate manually"
      git rebase --abort || true
      return 1
    fi
  done
  return 0
}

log "git fetch + rebase onto origin/$BRANCH"
git fetch origin "$BRANCH" || fail "git fetch failed"
git checkout "$BRANCH" || fail "git checkout failed"
git pull --rebase origin "$BRANCH" || fail "git pull --rebase failed"

if [ ! -f "$BACKTESTS_FILE" ]; then
  log "no $BACKTESTS_FILE — nothing to run"
  write_status "OK" "no backtests configured"
  commit_and_push_status_only || true
  exit 0
fi

ran_any=0
any_failed=0
while IFS='|' read -r strategy params from_ts to_ts source; do
  case "${strategy:-}" in
    ''|\#*) continue ;;
  esac
  strategy="${strategy// /}"
  params="${params// /}"
  from_ts="${from_ts// /}"
  to_ts="${to_ts// /}"
  source="${source// /}"

  log "backtest: $strategy ($from_ts → $to_ts)"
  if ! python -m trading.cli.backtest \
      --strategy "$strategy" \
      --params "$params" \
      --from "$from_ts" --to "$to_ts" \
      --source "$source"; then
    log "  FAILED — continuing with next"
    any_failed=1
    continue
  fi

  log "  export markdown summary"
  if ! python "$REPO/scripts/export_result_md.py" --strategy "$strategy"; then
    log "  export failed"
    any_failed=1
    continue
  fi
  ran_any=1
done < "$BACKTESTS_FILE"

# Decide final status.
if [ "$any_failed" -eq 1 ] && [ "$ran_any" -eq 0 ]; then
  write_status "FAIL" "todos los backtests fallaron"
elif [ "$any_failed" -eq 1 ]; then
  write_status "OK" "con fallos parciales (ver log) — al menos 1 backtest exportado"
elif [ "$ran_any" -eq 0 ]; then
  write_status "OK" "nada que correr (lista vacía o sólo comentarios)"
else
  write_status "OK" "todos los backtests exportados"
fi

# Stage result files + status. Humans own everything else under estrategias/.
git add estrategias/resultados/ || true
if git diff --cached --quiet; then
  log "no changes to commit"
  exit 0
fi

COMMIT_MSG="chore(research): auto-export backtest results $(date -u +%Y-%m-%d)"
git commit -m "$COMMIT_MSG" || fail "git commit failed"

if ! push_with_backoff; then
  fail "push failed after retries"
fi

log "done"
