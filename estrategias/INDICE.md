# Índice de estrategias

Formato: `estado | nombre | family | último verdict | último resultado (fecha) | 1-línea`.
Orden: activas → en-desarrollo → descartadas. Mantener ≤ 1 pantalla.

## Activas

| nombre | family | último verdict | último resultado | resumen |
|---|---|---|---|---|
| last_90s_forecaster_v3 | polymarket_btc5m | activa (paper, gate-bypass declarado) | 2026-04-25 21:01 UTC | v3_priceshist en paper. AUC single-split 0.7311 + microstructure provider wired. WF 3 folds inconcluso (B); promoción es bypass consciente del gate. Revertir si paper PnL trailing 7d ≤ 0 o WR < 50% sobre ≥30 fills. `.md` físico vive en `en-desarrollo/last_90s_forecaster_v3.md` (deuda: mover a `activas/`). |
| trend_confirm_t1_v1 | polymarket_btc5m | activa (paper) | — | Corre en paper (`staging.toml`) y dispatch en `cli/backtest.py` + `cli/paper_engine.py`. `.md` ausente — deuda institucional. |

## En desarrollo

| nombre | family | último verdict | último resultado | resumen |
|---|---|---|---|---|
| bb_residual_ofi_v1 | polymarket_btc5m | falsificada (WF 4×5d, 2026-04-27) | 2026-04-27 04:22 UTC | WF Platt 4×5d: mean_auc 0.463, stability_index 0.25 (cap 0.60), lift vs v3 −19.6 pp; fold 1 anti-señal AUC 0.318. Falla criterios 1 y 4 del propio `.md`. v2 despromovido, engine reiniciado, `shadow_mode_no_model`. Pendiente mover a `descartadas/`. |
| cvd_confirm_t2_v0 | polymarket_btc5m | — (doc-only) | — | CVD 1m como 7º gate de confirmación sobre trend_confirm_t1_v1. Solo `.md`; no hay `.py` ni `.toml` todavía. |
| oracle_lag_v1 | polymarket_btc5m | edge_likely (paper_ticks) | 2026-04-26 | Cesta Binance/Coinbase + USDT basis + Φ(δ/σ√τ). 7 sprints completados (ADR 0013); Sharpe/trade 0.515 sobre paper_ticks 11h, permutation pv=0. Promovido 2026-04-26 a paper activo (shadow=false, stake $2). |

## Descartadas

| nombre | family | motivo | resumen |
|---|---|---|---|
| _forensics_trend_confirm_t1_v1 | (informe forense) | fix aplicado y validado 2026-04-25 | Bug en `paper/backtest_loader` (1m+open) + `engine/backtest_driver` (settle canónico vía `market_outcomes`); re-run 23-abr 12-18 UTC pasó de 9.7 % → 63.2 % win, pnl +$44. |
| _audit_polybot_groundtruth | (informe forense) | fix aplicado 2026-04-25 — caso cerrado | 40.5 % labels invertidas en training set v2; `_load_resolved_markets` re-deriva open/close desde Binance 1 m; activo polybot-trained queda flageado en `metrics.ground_truth_audit`. |
| last_90s_forecaster_v2_bbres | polymarket_btc5m | falsificado 2x — labels biased y limpias | Lift 0.000 pp AUC en ambos regímenes; construcción colapsada por hardcoding de `implied_prob_yes` en training. Re-abrir requiere libro PM histórico ingerido al training. |
| last_90s_forecaster_v1 / v2 / contest_ensemble_v1 / contest_avengers_v1 | polymarket_btc5m | eliminadas 2026-04-26 por decisión del usuario | v1 WR 28.7 % positivo por payoff asimétrico, no edge real. v2 reemplazada por v3 (mismo trainer, +5 microstructure features). contest_ensemble_v1 PnL -$260 / Sharpe negativo. contest_avengers_v1 0 trades. Código + configs + tests + dashboard contest_ab + ADR 0012 (superseded) limpiados. `LGBRunner` extraído a `_lgb_runner.py`. |
| oracle_lag_v2 | polymarket_btc5m | falsificada 2026-04-27 — ceiling test | Maker-first Avellaneda-Stoikov sobre el scoring core de v1 (ADR 0014, SUPERSEDED). Backtest A/B 8 días, 2118 markets, asunción ideal-maker (fee=0, fill=100 %): v2 avg PnL/trade $0.68 vs v1 $11.96 → gate ADR 0014 (#1: ≥ v1+1.5 ¢) falla por orden de magnitud. La señal Φ(δ/σ√τ) no es invariante en el tiempo dentro del market; ampliar window 60-297s degrada selectividad. v1 queda como implementación correcta. |

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
