# Índice de estrategias

Formato: `estado | nombre | family | último verdict | último resultado (fecha) | 1-línea`.
Orden: activas → en-desarrollo → descartadas. Mantener ≤ 1 pantalla.

## Activas

_(ninguna todavía — las que pasen criterios se listan aquí)_

## En desarrollo

| nombre | family | último verdict | último resultado | resumen |
|---|---|---|---|---|
| cvd_confirm_t2_v0 | polymarket_btc5m | — | — | CVD 1m como 7º gate de confirmación sobre trend_confirm_t1_v1. |
| last_90s_forecaster_v3 | polymarket_btc5m | validado | 2026-04-25 (lift +10.5 pp AUC) | v2 + 5 microstructure tail (CVD, signed autocorr, intensity, taker, large). 3/5 en top-10 importance. Subset honesto por retention 90 d crypto_trades. |

## Descartadas

| nombre | family | motivo | resumen |
|---|---|---|---|
| _forensics_trend_confirm_t1_v1 | (informe forense) | fix aplicado y validado 2026-04-25 | Bug en `paper/backtest_loader` (1m+open) + `engine/backtest_driver` (settle canónico vía `market_outcomes`); re-run 23-abr 12-18 UTC pasó de 9.7 % → 63.2 % win, pnl +$44. |
| _audit_polybot_groundtruth | (informe forense) | fix aplicado 2026-04-25 — caso cerrado | 40.5 % labels invertidas en training set v2; `_load_resolved_markets` re-deriva open/close desde Binance 1 m; activo polybot-trained queda flageado en `metrics.ground_truth_audit`. |
| last_90s_forecaster_v2_bbres | polymarket_btc5m | falsificado 2x — labels biased y limpias | Lift 0.000 pp AUC en ambos regímenes; construcción colapsada por hardcoding de `implied_prob_yes` en training. Re-abrir requiere libro PM histórico ingerido al training. |

---

**Prioridad de la sesión actual:** walk-forward 3 × 7 d de
`last_90s_forecaster_v3` antes de promotion. Bloqueador: retention
90 d de `market_data.crypto_trades` permite hoy sólo 1 × 7 d (no
representativo). Esperar a ≥ 2026-05-13 para el 3 × 7 d completo, o
extender retention via ADR (decisión de Hector). En el mientras
tanto: shadow paper de v3 ya activo; revisar feature importance
estabilidad y desviación de calibración cada 24 h. Segunda prioridad
pendiente: extender `PolybotSQLiteLoader` con
`provides_settle_prices=True` + `market_outcomes()` leyendo
`crypto_ohlcv` 1 m (deuda registrada en BITACORA 25-abr).
