#!/usr/bin/env bash
# Walk-forward por slicing manual para oracle_lag_v1 (Sprint C).
#
# La estrategia es analítica (Φ(δ/σ√τ)) — no requiere refit. Sólo
# necesitamos 5 ventanas non-overlapping y comparar realized PnL /
# Sharpe entre folds para medir estabilidad temporal.
#
# Loader: polybot-agent.db (8 días disponibles, 2026-04-18 → 04-26).
# Folds: 5 × ~1.5 días non-overlapping.
#
# Stability index = 1 - cv (coefficient of variation) sobre PnL realized.
# Verdict:
#   stable     → cv < 0.30 AND todos los folds edge_likely.
#   inconclusive → cv 0.30-0.60.
#   unstable   → cv > 0.60.

set -uo pipefail

PARAMS="config/strategies/pbt5m_oracle_lag_v1.toml"
LOADER="/btc-tendencia-data/polybot-agent.db"
RESULTS="/tmp/wf_oracle_lag_$(date -u +%Y%m%dT%H%M%SZ).tsv"

echo -e "fold\tfrom\tto\tn_trades\trealized\twr\tsharpe\tpv\tverdict" > "$RESULTS"

declare -a folds=(
  "1|2026-04-18T03:00:00Z|2026-04-19T15:00:00Z"
  "2|2026-04-19T15:00:00Z|2026-04-21T03:00:00Z"
  "3|2026-04-21T03:00:00Z|2026-04-22T15:00:00Z"
  "4|2026-04-22T15:00:00Z|2026-04-24T03:00:00Z"
  "5|2026-04-24T03:00:00Z|2026-04-26T03:00:00Z"
)

for fold in "${folds[@]}"; do
  IFS='|' read -r idx from to <<< "$fold"
  echo "[fold $idx] $from → $to" >&2

  out=$(docker compose -f /home/coder/Trading-Edge-App/docker-compose.yml exec -T tea-engine \
    python -m trading.cli.mc \
    --strategy polymarket_btc5m/oracle_lag_v1 \
    --params "$PARAMS" \
    --from "$from" --to "$to" \
    --source polybot_sqlite \
    --polybot-db "$LOADER" \
    --slug-encodes-open-ts \
    --kind bootstrap --n-iter 500 2>&1 | grep -E "mc.realized.done|mc.bootstrap.done")

  n_trades=$(echo "$out" | grep "mc.realized.done" | grep -oP '"n_trades": \K[0-9]+' | head -1)
  pv=$(echo "$out" | grep "mc.bootstrap.done" | grep -oP '"permutation_pvalue": \K[0-9.]+' | head -1)
  verdict=$(echo "$out" | grep "mc.bootstrap.done" | grep -oP '"verdict": "\K[a-z_]+' | head -1)

  # mc.realized.done doesn't carry realized PnL directly; pull from DB.
  realized=$(docker compose -f /home/coder/Trading-Edge-App/docker-compose.yml exec -T tea-postgres bash -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT realized->>'\''total_pnl'\'' FROM research.mc_runs ORDER BY started_at DESC LIMIT 1"' 2>/dev/null)
  wr=$(docker compose -f /home/coder/Trading-Edge-App/docker-compose.yml exec -T tea-postgres bash -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT realized->>'\''win_rate'\'' FROM research.mc_runs ORDER BY started_at DESC LIMIT 1"' 2>/dev/null)
  sharpe=$(docker compose -f /home/coder/Trading-Edge-App/docker-compose.yml exec -T tea-postgres bash -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT realized->>'\''sharpe_per_trade'\'' FROM research.mc_runs ORDER BY started_at DESC LIMIT 1"' 2>/dev/null)

  echo -e "$idx\t$from\t$to\t$n_trades\t$realized\t$wr\t$sharpe\t$pv\t$verdict" >> "$RESULTS"
done

echo
echo "results in $RESULTS"
column -t -s $'\t' "$RESULTS"
