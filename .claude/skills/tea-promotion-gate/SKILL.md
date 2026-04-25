---
name: tea-promotion-gate
description: >
  Pre-flight checklist before flipping research.models.is_active=true on
  any polymarket_btc5m strategy. Hard offline gates (AUC, Brier, ECE),
  walk-forward stability, sample-size adjustments, and paper PnL signal
  required before promotion. Codifies ADR 0011 + the lessons from the v2
  bb_residual falsification and the v3 shadow lifecycle. Invoke when
  user says "promote v3", "set is_active=true", "ready to flip live",
  or asks "can we promote this model".
---

`is_active=true` means the strategy issues binding ENTER decisions in
paper. **Never flip without all four sections passing.** Below are the
exact thresholds, the source of truth for each, and the SQL/CLI that
verifies them.

## Section 1 — offline hard gates

From ADR 0011 + sample-size-aware caps applied in
`src/trading/cli/train_last90s.py`. All three must hold on the held-out
test set.

| metric | threshold (n_test ≥ 200) | threshold (n_test < 200) | source |
|---|---|---|---|
| AUC_test | ≥ 0.55 | ≥ 0.55 (no relaxation) | `metrics.test_auc` |
| Brier_test | ≤ 0.245 | ≤ 0.260 | `metrics.test_brier` |
| ECE_val | ≤ 0.05 (post-iso) | ≤ 0.20 | `metrics.ece_val` |

Verify:

```sql
SELECT version, metrics->>'test_auc' AS auc,
       metrics->>'test_brier' AS brier,
       metrics->>'ece_val' AS ece,
       metrics->>'n_test' AS n_test, is_active
FROM research.models
WHERE name = '<strategy_name>'
ORDER BY trained_at DESC LIMIT 5;
```

If any gate fails → strategy stays shadow. Don't relax thresholds; if
the threshold is wrong for this strategy, that's a separate ADR.

## Section 2 — walk-forward stability

A single train/test split can flatter a model. Walk-forward 3 × 7 d
verifies temporal stability.

```bash
docker compose exec tea-engine python -m trading.cli.walk_forward \
    --strategy <name> --folds 3 --fold-days 7 --min-train-days 14
```

Pass criteria:

- `stability_index ≥ 0.6` where stability_index =
  `1 - std(AUC_per_fold) / mean(AUC_per_fold)`.
- Median fold AUC ≥ 0.55.
- No fold AUC < 0.50 (a fold worse than coin flip kills promotion —
  the model has unstable regimes).

The walk-forward CLI writes one row per fold to
`research.walk_forward_runs`; aggregate by `model_version`.

**Crypto_trades retention caveat (TEA-specific)**: walk-forward 3 × 7
days requires ≥ 21 days of `market_data.crypto_trades` history. The
table has 90 d retention; check current minimum `ts` before scheduling.

## Section 3 — paper-shadow signal

Strategies are born `shadow=true` in TOML. **Minimum 7 days** of shadow
predictions logged to `trading.fills` (or
`research.paper_predictions` if using the dedicated table) before
promotion.

```sql
-- Count shadow predictions per day for the candidate strategy.
SELECT date_trunc('day', f.ts) AS day, count(*) AS n_predictions,
       avg(CASE WHEN (f.metadata->>'realized_won')::bool THEN 1 ELSE 0 END) AS win_rate
FROM trading.fills f
JOIN trading.orders o ON f.order_id = o.id
WHERE o.strategy_id = '<strategy_id>'
  AND f.ts >= now() - interval '14 days'
GROUP BY day ORDER BY day;
```

Pass criteria:

- ≥ 7 distinct days with predictions
- ≥ 50 predictions total (otherwise the win-rate CI is too wide)
- Realized win-rate ≥ 0.50 (no edge → no promotion, even if offline AUC
  is great — proxy/distribution gap)
- Paper PnL trailing 14 d ≥ $0 net of fees

If `_daily_pnl` reconciliation flags appear (search logs for
`reconciliation.fail`), resolve those before evaluating shadow PnL —
stale bootstrap state can hide a problem.

## Section 4 — operational pre-flight

- The active model row is unique. The partial-unique index
  `models_active_uk` enforces it. Flipping a new row to `is_active=true`
  requires flipping the existing one to false first, in the same
  transaction:
  ```sql
  BEGIN;
  UPDATE research.models SET is_active=false WHERE name=$1 AND is_active;
  UPDATE research.models SET is_active=true WHERE id=$2;
  COMMIT;
  ```
- The TOML `[paper].shadow` flag must change `true → false` in the
  same commit as the DB flip. Otherwise paper boots in shadow despite
  the active model.
- `staging.toml` must have `[strategies.<name>] enabled = true`.
- Restart `tea-engine`. Verify the new model loads:
  ```bash
  docker compose logs tea-engine --tail=200 | grep -i "<name>\|is_active"
  ```

## Sign-off

Document the promotion in `estrategias/en-desarrollo/<name>.md`
`## Historial` with the 4-section pass evidence (numbers, fold counts,
shadow window) and move the row in `estrategias/INDICE.md` from
`En desarrollo` to `Activas`. Then commit + push.

## Don't

- Don't promote on a single offline gate pass. The 2026-04-25
  `bb_residual` falsification existed precisely because the offline
  AUC looked fine on a biased construction; only the apples-to-apples
  walk-forward exposed it.
- Don't promote during a freeze window or just before an upstream
  oracle change (Polygon EAC, Binance fee tier, Polymarket fee
  schedule). Wait one week post-change for distribution stabilisation.
- Don't promote with `n_test < 50`. The threshold caps in section 1
  exist to *not block training*, not to license promotion on tiny
  splits.
