# Bitácora de investigación

Append-only. Ideas crudas sin estrategia asignada aún. Cuando una entrada
madura, se convierte en archivo bajo `en-desarrollo/` y se borra su
párrafo de acá.

Formato: `## YYYY-MM-DD — tema corto` + 1-5 líneas.

---

## 2026-04-27 — bb_residual_ofi_v1 falsificada por walk-forward (Platt)

Cierre del ciclo bb_ofi. Cambios introducidos antes de evaluar:
isotónica → Platt en `train_last90s.train()` (vía nuevo flag
`--calibration`) y en `train_bb_ofi_ensemble`; helper en
`src/trading/research/calibration.py`. Trainer extendido con
`--walk-forward --wf-folds N --wf-fold-days D`. v2
(`bb_ofi_2026-04-26T02-46-53Z`) despromovido en
`research.models` por SQL (ECE 0.215, 4× sobre cap ADR 0011).
Engine reiniciado 04:23 UTC; bb_ofi en `shadow_mode_no_model`.

Run: 35 d, n=8305, 4 × 5 d, 25 trials / 180 s, Platt.

| fold | AUC | Brier | ECE |
|---|---:|---:|---:|
| 0 | 0.589 | 0.281 | 0.182 |
| 1 | **0.318** | 0.370 | 0.373 |
| 2 | 0.456 | 0.321 | 0.262 |
| 3 | 0.488 | 0.244 | 0.050 |

mean_auc 0.463, stability_index **0.25** (cap 0.60), lift vs v3
(0.659) = **−19.6 pp**. Falla criterios 1 y 4 del propio `.md`.
Fold 1 AUC 0.318 = anti-señal: el modelo predice al revés en esa
ventana. Confirma que el AUC 0.597 del split sequential del 26-abr
era artefacto de ventana, no señal generalizable. Platt no salva
modelos con AUC < 0.5 — confirma que el problema es feature set,
no calibrador.

Decisión: pendiente confirmación del usuario para mover `.md` a
`descartadas/` y desmontar dispatch + staging.toml + tests via
`tea-strategy-removal`. v2 ya inerte (no-active-row).

Lo que sobrevive: pipeline WF reusable para futuras estrategias
del family; `PlattCalibrator` con `.predict([p])` API.

## 2026-04-27 — oracle_lag_v2 descartada (ceiling test)

Antes de invertir en el wiring `LimitBookSim ↔ SimulatedExecutionClient`
(Sprint D+1 del ADR 0014), corrimos backtest A/B con asunción
ideal-maker (fee=0, fill=100 %) sobre 8 días / 2118 markets en
`polybot-agent.db`: v2 avg PnL/trade $0.68 vs v1 $11.96. **Gate #1
de ADR 0014 (`v2 ≥ v1+1.5 ¢`) falla por orden de magnitud aun bajo
el techo absoluto de la hipótesis.** Causa raíz: la señal Φ(δ/σ√τ) no
es invariante en el tiempo dentro del market; ampliar la ventana de
entrada a [60,297]s diluye el edge predictivo (a t=60s la incertidumbre
del residual es ~3× mayor que a t=285s). Corolario: el rebate maker
teórico era <0.5 % del PnL total de v1, no el +30-50 % proyectado.
**Aprendizaje permanente**: una política de ejecución (taker→maker)
no se evalúa como "mismo edge, fee distinta" — la ventana condiciona
la calidad de la señal. ADR 0014 marcado SUPERSEDED. v1 queda como
implementación canónica del residual sobre cesta multi-CEX. Limpieza
del repo: borrados `oracle_lag_v2.py`, `pbt5m_oracle_lag_v2*.toml`,
`test_oracle_lag_v2.py`, `engine/avellaneda_stoikov.py` + test (sin
otros consumidores). Dispatch unhooked en backtest/paper_engine/mc.

---

## 2026-04-26 — limpieza de estrategias muertas

Eliminadas del repo: `last_90s_forecaster_v1`, `last_90s_forecaster_v2`,
`contest_ensemble_v1`, `contest_avengers_v1`. Decisión del usuario por
métricas pobres (v1 WR 28.7 %, contest_ensemble PnL -$260, contest_avengers
0 trades). v2 reemplazada por v3 — mismo trainer, +5 features microstructure.
Refactor previo: `LGBRunner` extraído de v2 a
`src/trading/strategies/polymarket_btc5m/_lgb_runner.py` (ahora compartido por
v3 + bb_residual_ofi_v1). `cli/contest_ab_weekly.py`, dashboard `contest_ab.json`,
`scripts/grid_search_v1_divisor.py` y los `resultados/*` correspondientes
borrados. ADR 0012 queda como historial-superseded.

---

## 2026-04-25 — promoción v3_priceshist a paper (bypass gate, riesgo declarado)

`is_active=true` flippeado en `research.models` para
`v3_priceshist_2026-04-25T12-16-50Z`. `shadow=false` en TOML.
**Bypass consciente del `tea-promotion-gate`**: WF fue B (no A);
≥ 7 d de shadow no se cumplen. Justificación: es paper, no real
money. Wiring pre-promotion: `_microstructure_provider.py` nuevo +
`paper_engine.py` lo construye al boot, refresca cada 5 s en
`_shared_providers_refresh_loop`, lo pasa a `Last90sForecasterV3`. Sin
esto el strategy serving emitía sentinels en el tail de microstructure
= train/serve skew silencioso. Restart 21:01 UTC primer boot, luego
21:05 (rebuild de imagen). Logs: `microstructure_provider.ready` ✓,
`v3.no_active_model_row` ausente ✓. Monitoring SQL + revert criterion
documentados en `estrategias/en-desarrollo/last_90s_forecaster_v3.md::Promotion 2026-04-25`.

---

## 2026-04-25 — walk-forward v3_priceshist + v2_baseline (outcome B)

Backfill aggTrades (8709 markets, 9.47M rows en ~50 min sobre la
ventana 2026-03-22→2026-04-22) + restart tea-ingestor zombie + extend
`walk_forward.py` con dispatch v3 + run 3 folds (IS=4d/OOS=1d) sobre
los 7 d polybot-agent disponibles. Resultado: ningún fold entrena
honestamente — 2 unvalidated (sample drops), 2 trivial (AUC train=0.5),
1 marginal (n=23 AUC OOS 0.59). **Outcome B — hold/iterate**, NO
promotion. Bloqueos no del modelo: prices_history sólo cubre 4/21-4/25
(fold 0 v3 = IS=0), gap crypto_trades 4/25 00:00→15:32 (fold 2 v3
n_oos=3). Para WF válido: extender prices_history a 4/04-4/20 +
backfill aggTrades del slice 4/24 23:59→4/25 15:32. Detalle en
`estrategias/en-desarrollo/last_90s_forecaster_v3.md::Walk-forward 2026-04-25`.

---

## 2026-04-25 — skills instaladas

Bundled (vienen con Claude Code): simplify, review, security-review, init,
loop, schedule, claude-api, update-config, keybindings-help,
fewer-permission-prompts. Custom TEA bajo `.claude/skills/`:
strategy-template, forensics, promotion-gate, backfill-pattern. Cada
custom referencia los scripts/ADRs reales del proyecto. Marketplace
Anthropic + community skills (test-driven-development,
software-architecture, prompt-engineering) requieren `/plugin
marketplace add anthropics/skills` ejecutado por el usuario; agente no
puede invocar slash-commands de plugin.

---

## 2026-04-25 — TAREA 1+2+3 stabilization sprint

Tres deudas operacionales resueltas + ingest nuevo:
- TAREA 1: tea-engine restart, v3 entró en RAM (`v3.no_active_model_row` log al boot, `paper.driver.bootstrap_daily_pnl` por strategy con sumas reales del trailing 24 h).
- TAREA 2.4: `paper/driver.py` bootstrap de `_daily_pnl` desde `trading.fills` 24 h + filtro `strategy_id` en `_reconciliation_loop` SQL. Suprime los falsos positivos post-restart documentados como deuda el 25-abr.
- TAREA 2.5: `PolybotSQLiteLoader` ahora setea `provides_settle_prices=True` y `market_outcomes()` lee `crypto_ohlcv` 1 m al `close_time`. Backtests `--source polybot_sqlite` settlean canonical (igual que paper_ticks post fase 1).
- TAREA 2.6: 11 filas históricas en `research.backtests` con `data_source='polybot_sqlite'` taggeadas con `metrics.audit_flag = "POLYBOT_SETTLE_CHAINLINK_PRE_FIX_2026-04-25, ..."`.
- TAREA 3.7: nueva tabla `market_data.polymarket_prices_history` (hypertable) — schema en `infra/postgres/init/11_polymarket_prices_history.sql`.
- TAREA 3.8: `scripts/backfill_polymarket_prices_history.py` con UA Chrome (Cloudflare 1010 bypass), idempotente vía PK + `_condition_ids_already_done`. Backfill en curso para los 865 markets BTC up/down 5m en 2026-04-22..2026-04-25.
- TAREA 3.9: `train_last90s` añade flag `--use-real-implied-prob` que reemplaza el hardcode 0.5 con la price-history real, drop si no hay row.
- TAREA 3.10 (re-train v3 con `--use-real-implied-prob`):
  **completado**. Backfill final 2.45 M rows / 865 markets (full
  coverage). v3_priceshist_2026-04-25T12-16-50Z, n=149 (104/22/23):
  **AUC=0.7311** vs v3_first 0.6591 → **lift +7.2 pp**.
  Brier=0.252 vs 0.236 (+1.6 pp leve degradación), ECE=0.173 vs 0.147
  (+2.6 pp). `implied_prob_yes` aparece **rank #2** del importance
  (14.2 % gain) en v3_priceshist contra 0 % en v3_first (constante).
  passes_gate=true. is_active=false (no promoción esta sesión).
  Top-10 v3_priceshist: m30_bps, implied_prob_yes, bm_cvd_normalized,
  bm_signed_autocorr_lag1, adx_14, bm_trade_intensity, hour_sin,
  ema8_vs_ema34_pct, m60_bps, bm_large_trade_flag.

Pre-trabajo: resolver FAIL del VPS (uncommitted local mods bloqueando rebase).
Hicimos 7 commits temáticos + ff-merge feature → main + push, restaurando OK.
VPS post-fix: status OK, próximo cron 06:00 UTC mañana.

## 2026-04-24 — inicialización

Flujo de iteración de estrategias formalizado en `estrategias/`. Código
sigue viviendo en `src/trading/strategies/`, configs en `config/strategies/`.
Esta carpeta es sólo doc + resultados resumidos en markdown.

## 2026-04-24 — forense trend_confirm FAIL

FAIL `21dcdc91-…` (paper_ticks 6 h, win 9.7 %) vs MARGINAL `1833b654-…`
(polybot_sqlite 6 d, win 76.1 %) son la misma estrategia, mismo
params_hash. Bug estructural en `paper/backtest_loader.py` (open_price
indexado al 5 m close = precio en window_close, no window_open) +
`engine/backtest_driver.py` (`_final_price_of` usa chainlink congelado).
Detalle y fix propuesto en `en-desarrollo/_forensics_trend_confirm_t1_v1.md`.
Detenido fase 2 (`bb_residual`) hasta confirmación.

## 2026-04-25 — fix forense aplicado y validado

Loader paper_ticks ahora usa Binance 1 m + columna `open` para el
strike y expone `market_outcomes` (1 m close al `close_time`,
mismo path que `backfill_paper_settles`); driver dispatcha por
`provides_settle_prices`. Re-run 23-abr 12-18 UTC: 38 trades,
**win 63.2 %**, pnl +$44.10. Banda esperada [55 %, 65 %] cumplida.
Forense movido a `descartadas/`. Próxima sesión libera fase 2
(`bb_residual`) sobre `last_90s_forecaster_v2`.

## 2026-04-25 — fase 2 bb_residual falsificada

Implementada `bb_residual` (4 features tail) y entrenado v2_clean +
v2_bbres mismo seed/dataset (n=507). Métricas idénticas hasta el último
dígito → lift 0.000 pp. Detalle en
`en-desarrollo/last_90s_forecaster_v2_bbres.md`. Causa: hardcoding
de `implied_prob_yes=0.5` en `train_last90s.build_samples` colapsa
3/4 features bb a transformaciones lineales de bb_p_prior (que es
función de spot/open/rv ya codificados). Deuda registrada para
sesión separada: (a) auditar polybot SQLite por patrón
"open_price = chainlink frozen" (heredado del análisis del 24-abr,
no se tocó polybot), y (b) ingerir libro Polymarket histórico para
desbloquear `bb_market_vs_prior` real.

## 2026-04-25 — audit polybot ground truth: SESGADO

40.5 % de las labels del training set de v2 (185/457) están invertidas
respecto a Binance OHLCV 1 m. 49.9 % de markets tienen
`open_price == first_chainlink_price` y 47 % tienen un único valor de
chainlink en los 5 min. AUC=0.664 del modelo activo no es interpretable.
Detalle: `en-desarrollo/_audit_polybot_groundtruth.md`. Repro:
`scripts/audit_polybot_groundtruth.py` (read-only). Recomendación:
re-derivar `close_price` y `open_price` desde Binance OHLCV en
`train_last90s._load_resolved_markets` antes de cualquier re-train.

## 2026-04-25 — restart tea-engine para aplicar despromoción v2

[restart] tea-engine para aplicar despromoción v2 en RAM. Errores
`reconciliation_fail` [transient]: 40 eventos en 40 min con shape
único (`db_settled` rolling 24h vs `local` ledger del driver tras
boot), todos coincidiendo con el restart anterior a las 03:57:00 UTC
y sin stack trace ni excepción — el ALERT-only loop de
`paper/driver.py:687` reporta divergencia esperada post-boot.
Restart 05:11:52 UTC: v2 logueó `v2.no_active_model_row` al boot,
luego 10/10 ticks en window [205,215] SKIPped con
`shadow_mode_no_model`; `last_90s_forecaster_v1` y
`contest_ensemble_v1` registraron fills nuevos.

## 2026-04-25 — re-validación side-quest reconciliation_fail

Investigación catalogada formal: TRANSIENT BENIGNO (caso A). 40/40
eventos pre-restart son `paper.reconciliation.fail`, 0 `reconciliation.ok`;
mismo shape numérico repetido por 5 strategies × 8 ciclos. Path:
`_reconciliation_loop` alert-only por diseño. NO se ejecutó nuevo
restart (engine ya rebootado a 05:11:52 en sesión previa). Engine
post-restart healthy: 4 fills en 5 min (v1+ensemble), 5782 paper_ticks/min,
v2 shadow confirmado, 0 errores no-reconciliation.

## 2026-04-25 — restart tea-engine para activar v3 en RAM

v3 dispatch añadido en commit anterior pero engine no había reiniciado
desde 2026-04-25 05:11. Restart aplicado a 06:05:31 UTC tras dos
intentos previos: el primero faltó `staging.toml` con bloque v3
(docker cp lo había ignorado), el segundo había desactivado las otras
4 strategies por uncommitted changes en host. Tras fix, v3 ahora en
shadow corriendo (`v3.no_active_model_row` + `top_reasons:
[["shadow_mode_no_model", 8]]`). Persistencia de predicciones: solo
logs (Decision.signal_features descartado en paper driver).

## 2026-04-25 — plan v3 hasta walk-forward

v3_first validado en n=149 (lift +10.5pp AUC vs v2_baseline, 3/5
microstructure features en top-10). NO iterar features hasta
2026-05-13 cuando crypto_trades cubra 3 semanas. v3 queda en shadow.
Trabajo paralelo: abrir ADR ingest libro Polymarket histórico.

## 2026-04-25 — v3 microestructura Binance: hipótesis validada

`last_90s_forecaster_v3` = v2 base + 5 features de Binance taker tape
sobre ventana 90 s. Subset honesto (RAMA Y por retention 90 d de
`crypto_trades`): n=149 (104 train / 22 val / 23 test). Lift AUC
**+10.5 pp** vs v2_baseline mismo subset/seed (0.5444 → 0.6591),
Brier −4.7 pp, ECE −5.6 pp. 3/5 features microstructure en top-10
importance (CVD #1 23.7 %, signed autocorr #2 19.1 %, intensity #7
4.4 %). Detalle en `en-desarrollo/last_90s_forecaster_v3.md`.
**Próximo paso**: walk-forward 3 × 7 d cuando crypto_trades alcance
3 semanas de retention (~13 mayo). v3 nace shadow,
`is_active=false`.

## 2026-04-25 — deuda: reconciliation.fail post-restart

El loop `_reconciliation_loop` (`paper/driver.py:687-739`) compara
rolling 24h DB vs `_daily_pnl` en RAM. Tras restart, RAM arranca
en 0 y DB en pre-restart settled, generando 1 fail × strategy
cada 5min × 24h ≈ 288 falsos positivos/strategy/restart. Riesgo:
normalización del alerta legítimo. Fix candidato (no aplicado):
bootstrap `_daily_pnl` en RAM al boot leyendo trailing 24h de
`trading.fills`, O suprimir alerta durante warmup window
(primeros 30 min post-boot). Decisión cuándo: cuando alguien
tenga 1h libre, no urgente.

## 2026-04-25 — despromoción last_90s_forecaster_v2 activo

Despromovido `v2_2026-04-23T20-06-38Z` (id `d70ba112-8b49-4b6a-b0bd-89372f61e621`,
AUC reportado 0.664, AUC real estimado ≈ 0.336 contra labels honestas)
tras audit polybot SESGADO. Modelo aprendió relación inversa por labels
contaminadas. Estrategia degrada a **shadow** automáticamente —
`load_runner_async` retorna `None` cuando no hay fila `is_active=true`,
y `should_enter` emite `Decision(SKIP, "shadow_mode_no_model")`. Cambio
**no surtirá efecto en el engine corriendo** hasta el próximo restart
(modelo cacheado en memoria, no hay reload periódico). No re-promotion
hasta v3 con features nuevos.

## 2026-04-25 — fix labels OHLCV aplicado; v2 falsificado contra ground truth real

`_load_resolved_markets` ahora re-deriva `open_price` / `close_price`
desde `crypto_ohlcv` 1 m al `minute(open_ts)` / `minute(close_ts)`.
Markets sin OHLCV en algún extremo se descartan (13 % loss, lejos del
50 % de stop). Re-train con seed 42, mismo budget:
**v2_clean_ohlcv AUC 0.430**, **v2_bbres_ohlcv AUC 0.430** (lift
0.000 pp por construcción colapsada). El feature set actual de v2 no
separa contra labels honestas. Activo `v2_2026-04-23T20-06-38Z`
sigue `is_active=true` pero con `metrics.ground_truth_audit` flag —
decisión de despromoción es de Hector. Audit y bbres .md movidos a
`descartadas/`. Detalle del re-train en
`descartadas/_audit_polybot_groundtruth.md` sección "Fix aplicado".

## 2026-04-25 — deudas registradas (descubiertas en paso 0 del re-train)

a. **Extender `PolybotSQLiteLoader`** con `provides_settle_prices=True`
   + `market_outcomes(...) -> dict[str, float]` leyendo
   `market_data.crypto_ohlcv` 1 m al `polymarket_markets.close_time`.
   Patrón idéntico al de `PaperTicksLoader` en fase 1, ~30 líneas
   aditivo. Hasta que se cierre, **todos** los backtests con
   `--source polybot_sqlite` settlean contra `_final_price_of(ticks)`
   que lee `last.chainlink_price` de polybot — la misma fuente que el
   audit del 25-abr mostró 40 % invertida.

b. **Auditar `research.backtests` filtrando `data_source =
   'polybot_sqlite'`** (o lo que represente esa fuente en el campo
   `data_source`) e identificar qué corridas reportaron métricas que
   pudieron influir decisiones (promotions, priorización de hipótesis).
   Candidato confirmado: `backtest_id = 1833b654-…` (trend_confirm_t1_v1,
   6 días, 76.1 % win 590 trades) — settle compuesto contra polybot
   chainlink, métrica no honesta. Decidir si re-correr con settle limpio
   tras (a), o anotar invalidación masiva en `_last_run_status.md`.
