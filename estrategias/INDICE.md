# Índice de estrategias

Formato: `estado | nombre | family | último verdict | último resultado (fecha) | 1-línea`.
Orden: activas → en-desarrollo → descartadas. Mantener ≤ 1 pantalla.

## Activas

_(ninguna todavía — las que pasen criterios se listan aquí)_

## En desarrollo

| nombre | family | último verdict | último resultado | resumen |
|---|---|---|---|---|
| bb_residual_ofi_v1 | polymarket_btc5m | — | — | BB no-drift prior + OFI/microestructura ensemble shrinkage; Sharpe-gated, fee convexa. Shadow scaffold (sin modelo entrenado). |
| cvd_confirm_t2_v0 | polymarket_btc5m | — | — | CVD 1m como 7º gate de confirmación sobre trend_confirm_t1_v1. |
| last_90s_forecaster_v3 | polymarket_btc5m | hold/iterate | 2026-04-25 (WF 3 folds: B — gaps datos, no promotion) | v2 + 5 microstructure + libro PM real. AUC 0.7311 single-split pero WF inconcluso (prices_history sólo 4/21-4/25 + gap crypto_trades 4/25). Requiere extender backfills antes de re-evaluar. |

## Descartadas

| nombre | family | motivo | resumen |
|---|---|---|---|
| _forensics_trend_confirm_t1_v1 | (informe forense) | fix aplicado y validado 2026-04-25 | Bug en `paper/backtest_loader` (1m+open) + `engine/backtest_driver` (settle canónico vía `market_outcomes`); re-run 23-abr 12-18 UTC pasó de 9.7 % → 63.2 % win, pnl +$44. |
| _audit_polybot_groundtruth | (informe forense) | fix aplicado 2026-04-25 — caso cerrado | 40.5 % labels invertidas en training set v2; `_load_resolved_markets` re-deriva open/close desde Binance 1 m; activo polybot-trained queda flageado en `metrics.ground_truth_audit`. |
| last_90s_forecaster_v2_bbres | polymarket_btc5m | falsificado 2x — labels biased y limpias | Lift 0.000 pp AUC en ambos regímenes; construcción colapsada por hardcoding de `implied_prob_yes` en training. Re-abrir requiere libro PM histórico ingerido al training. |

---

**Prioridad de la sesión actual:** esperar 2026-05-13 walk-forward
v3. Hasta entonces v3 corre shadow en paper (post-restart 25-abr ya
activo, vector 26 features). En la sesión post-2026-05-13: re-train
con `--use-real-implied-prob` sobre dataset extendido + walk-forward
CLI 3 × 7 d + decisión de promotion. Deuda completada en sprint del
25-abr: `PolybotSQLiteLoader` settle canónico, bootstrap `_daily_pnl`,
audit-flag de backtests `polybot_sqlite` source, ingest
`market_data.polymarket_prices_history` + train hook
`--use-real-implied-prob`.
