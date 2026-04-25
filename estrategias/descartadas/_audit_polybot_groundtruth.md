# Audit — polybot SQLite ground truth (read-only)

**No es estrategia. Informe puntual.** Prefijo `_` para que el INDICE
no lo confunda con una hipótesis viva.

Pregunta: ¿las labels y `open_price` que `trading.cli.train_last90s`
consume desde polybot SQLite tienen el mismo patrón estructural que
arreglamos en TEA fase 1 (open_price poblado con chainlink cuando spot
está vacío + chainlink congelado durante minutos)?

## Veredicto

**POLYBOT SESGADO.** Tres números clave:

| métrica | valor |
|---|---|
| % markets con `open_price == first_chainlink` | **49.9 %** (264 / 529) |
| markets con un solo valor distinto de `chainlink_price` en los 5 min | **247 / 529 = 46.7 %** |
| `label_stored ≠ label_canonical (Binance 1 m)` en training set v2 | **40.5 %** (185 / 457) |

40 % de las labels que entrenaron `v2_2026-04-23T20-06-38Z` (modelo
activo en `research.models`, AUC reportado 0.664) están **invertidas
respecto al ground truth real de Binance OHLCV** — es decir, el
modelo aprendió a predecir un mundo paralelo donde el oráculo lagueaba.

## Fuentes auditadas

- `/btc-tendencia-data/polybot-agent.db` (552 MB, BTC-Tendencia,
  `slug_encodes_open_ts=True`). Mount read-only en `tea-engine`. Único
  SQLite que aporta samples a `train_last90s` — la otra ruta
  (`/polybot-btc5m-data/polybot_agent.db`, mount default del CLI) no
  existe en este entorno (directorio vacío), de modo que el modelo
  activo, `v2_clean` y `v2_bbres` se entrenaron 100 % desde
  BTC-Tendencia.

- `market_data.crypto_ohlcv` (Postgres TEA), `interval='1m'`,
  `exchange='binance'`, `symbol='BTCUSDT'`. Misma fuente canónica que
  `scripts/backfill_paper_settles.py` y el fix forense de fase 1.

Schema confirmado vía `PRAGMA table_info`. **No hay tabla `markets`**
en polybot — la metadata se reconstruye desde `trades` + `ticks`,
exactamente como ya hace `train_last90s._load_resolved_markets`.

## Reproducción

```sh
docker compose exec tea-engine python scripts/audit_polybot_groundtruth.py
```

Read-only. Idempotente. Output es print-only — no escribe en ninguna
parte.

## Resultados detallados

### (a) `open_price` patterns

| | n | % |
|---|---:|---:|
| markets resueltos | 529 | 100 % |
| `open_price == first_chainlink` (±0.01) | 264 | **49.9 %** |
| `open_price == close_price` (±0.01) | 0 | 0 % |

La firma del bug TEA fase 1 ("open_price poblado con chainlink cuando
spot está vacío") aparece en exactamente la mitad de los markets de
polybot. No es coincidencia — es el mismo recorder pattern.

### (b) Distinct `chainlink_price` durante la ventana

Distribución sobre 529 markets resueltos:

- p10 = **1**
- p50 = 121
- p90 = 254
- p99 = 262
- promedio = 122 valores distintos

Buckets:

| bucket | n_markets |
|---|---:|
| 1 (chainlink congelado) | **247** |
| 2-5 | 11 |
| 6-20 | 1 |
| > 20 | 268 |

Distribución bimodal: 47 % de los markets tienen oráculo congelado
(literalmente un único valor en 300 segundos), el resto opera normal
(>20 valores). No hay una "cola gradual" — son dos regímenes.

### (c) Labels stored vs canónicas (Binance 1 m)

`label_stored = (close_price > open_price)` con `close_price = last
chainlink_price or last spot_price` (mismo cálculo que
`_load_resolved_markets`). `label_canon = (Binance 1 m close at
minute(close_ts) > Binance 1 m close at minute(open_ts))`.

| | valor |
|---|---:|
| n_eligible (OHLCV presente en ambos extremos) | 460 |
| n_skipped (gap de OHLCV) | 69 |
| n_match | 275 |
| n_disagree | **185** |
| **% disagree** | **40.2 %** |

40 % es astronómico. Para referencia, ruido aleatorio sobre BTC up/down
balanceado sería ~5 % (las únicas discrepancias legítimas son markets
donde el chainlink y el binance quedan en lados distintos del strike
por décimas de bps en la frontera).

### (d) Estratificación

Por bucket de `distinct_cl`:

| bucket | n | disagree | rate |
|---|---:|---:|---:|
| 1 (frozen) | 237 | 114 | **48.1 %** |
| 2-5 | 11 | 7 | 63.6 % |
| 6-20 | 0 | 0 | — |
| > 20 | 212 | 64 | **30.2 %** |

Por magnitud `|Binance 1 m close − Binance 1 m open|` en bps:

| bucket | n | disagree | rate |
|---|---:|---:|---:|
| [0, 1) | 45 | 21 | 46.7 % |
| [1, 5) | 165 | 76 | 46.1 % |
| [5, 20) | 222 | 79 | 35.6 % |
| [20, 50) | 28 | 9 | 32.1 % |
| [50, ∞) | 0 | 0 | — |

Lectura: chainlink congelado dispara el rate de disagreement al 48 %,
y el rate cae con la magnitud del movimiento (consistente con un
chainlink que laguea — los movimientos pequeños son los que se
flippean fácil). Pero **incluso el bucket >20 distintos tiene 30 %
disagree** — eso ya no es "chainlink congelado", es un sesgo más
amplio de calibración entre el oráculo de polybot y Binance.

### Blast radius en training set v2

Filtro temporal `2025-11-01 ≤ close_ts ≤ 2026-04-25` (idéntico a las
corridas `v2_2026-04-23T20-06-38Z` activo, `v2_clean` y `v2_bbres`):

| | valor |
|---|---:|
| n_train_eligible | 457 |
| n_train_disagree | 185 |
| **% disagree training set** | **40.5 %** |

Bien por encima del corte 5 %. La AUC=0.664 del activo y la AUC=0.534
de `v2_clean`/`v2_bbres` están mediendo predicción sobre labels
corruptas. **El "lift = 0" de bb_residual del ciclo anterior es
también irrelevante** — comparábamos dos modelos contra la misma
ground truth podrida.

## Recomendación

**Labels deben re-derivarse desde Binance OHLCV antes de cualquier
re-train de v2.** Cambio acotado: en
`src/trading/cli/train_last90s.py::_load_resolved_markets`, sustituir
el `close_price` polybot por `Binance 1m close at minute(close_ts)` —
mismo path que `scripts/backfill_paper_settles.py` y que el fix forense
de fase 1. La función ya recibe `close_ts` reconstruido del slug, y la
conexión a `crypto_ohlcv` ya existe en el CLI vía `_load_ohlcv_5m`.

Dejar `open_price` también re-derivado desde Binance 1 m al
`minute(open_ts)` (paralelo al fix de loader paper_ticks). Eso aborta
ambos vectores de contaminación en una sola pasada.

Costo estimado: 1 PR aditivo, ~30 líneas de código + tests (similar al
fix de fase 1). El impacto es: re-train v2 contra ground truth real,
medir AUC honesta, y sólo entonces reabrir bb_residual o cualquier
otra hipótesis de feature.

**No promover ni iterar nada hasta que esto se cierre.** Cualquier
métrica reportada del modelo activo (paper o live) es ruido sobre un
training set 40 % invertido.

## Fix aplicado (2026-04-25)

### Cambios

1. **`src/trading/cli/train_last90s.py`**:
   - Nueva helper `_fetch_ohlcv_1m_closes(pg_dsn, t_min_unix, t_max_unix)
     -> dict[int, float]` (psycopg2 sync, una sola query bulk al rango,
     indexado por minuto unix).
   - `_load_resolved_markets` cambia firma a
     `(sqlite_path, slug_encodes_open_ts, *, pg_dsn)`. Sigue usando
     polybot SQLite para descubrir qué markets resolvieron y derivar
     `open_ts` / `close_ts` desde el sufijo del slug, **pero
     sobreescribe `open_price` y `close_price` con
     `crypto_ohlcv` 1 m al `minute(open_ts)` y `minute(close_ts)`** —
     mismo path canónico que `scripts/backfill_paper_settles.py` y el
     fix forense fase 1 a `paper/backtest_loader.py`.
   - Política de gap: si falta candle 1 m en cualquier extremo, el
     market se DESCARTA (log INFO con conteo). No fallback a polybot.
2. **Callsites actualizados**:
   - `src/trading/cli/train_last90s.main()` — pg_dsn se construye antes
     de la carga de markets y se pasa a ambas llamadas.
   - `src/trading/cli/walk_forward.py::_eval_last_90s_v2_fold` — pasa
     `pg_dsn=_pg_dsn()`.
   - `scripts/grid_search_v1_divisor.py` — pg_dsn movido arriba para
     poder pasarlo. (No re-corrido en esta sesión, sólo wired.)
3. **`research.models` metadata**: las 6 filas v2_* (incluida la activa
   `v2_2026-04-23T20-06-38Z`) tienen `metrics.ground_truth_audit =
   "POLYBOT_SESGADO_2026-04-25, n_invertidas/n_total=185/457,
   audit:_audit_polybot_groundtruth.md"`. Aplicado vía `jsonb_set`,
   sin migración de schema. **Ningún `is_active` modificado** —
   decisión de despromoción/rollback queda con Hector.

### Cobertura OHLCV

`market_data.crypto_ohlcv` (Binance 1 m, BTCUSDT) cubre desde
2025-04-22 — anterior al inicio del training window (2025-11-01).
249 896 candles dentro del rango [2025-11-01, 2026-04-25). En el
re-train, **69 / 529 markets se descartaron por gap** (13.0 %), bien
por debajo del corte 50 % que detendría la sesión.

### Re-train con labels OHLCV (mismo seed=42, mismo budget)

| modelo | n_train | n_test | AUC | Brier | ECE | n_features | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| `v2_2026-04-23T20-06-38Z` (activo, polybot biased) | 134 | 30 | 0.664 | 0.253 | 0.108 | 21 | passed (biased) |
| `v2_2026-04-25T04-32-11Z` (clean, biased dataset 2x dup) | 354 | 77 | 0.534 | 0.234 | 0.115 | 21 | passed |
| `v2_bbres_2026-04-25T04-32-29Z` (bbres, biased dataset 2x dup) | 354 | 77 | 0.534 | 0.234 | 0.115 | 25 | passed |
| **`v2_2026-04-25T04-57-43Z` (clean OHLCV labels)** | 138 | 31 | **0.430** | 0.277 | 0.168 | 21 | **failed** |
| **`v2_bbres_2026-04-25T04-58-09Z` (bbres OHLCV labels)** | 138 | 31 | **0.430** | 0.277 | 0.168 | 25 | failed |

`v2_clean_ohlcv` y `v2_bbres_ohlcv` se entrenaron sobre 198 samples
(457 markets resueltos × build_samples filters), partidos
138/29/31. Estimación cruda del AUC del activo contra labels limpias
(`1 − 0.664 ≈ 0.336`) coincide con el orden de magnitud observado en
`v2_clean_ohlcv = 0.430`: el modelo activo aprendió la relación
**inversa** de polybot-chainlink-frozen, no la dinámica real de BTC.

### Lift bb_residual sobre labels limpias

**0.000 pp en AUC, Brier, ECE.** Idéntico al ciclo previo. La causa
es la misma — el feature `bb_market_vs_prior` colapsa a una función
lineal de `bb_p_prior` cuando training tiene `implied_prob_yes=0.5`
hardcodeado, y `bb_model_vs_prior ≡ 0` por construcción. La hipótesis
queda doblemente falsificada: ahora también contra labels honestas.

### Veredicto post-fix

- Bug de labels resuelto técnicamente: el código nunca más leerá
  `chainlink_price` polybot como ground-truth.
- Pero el modelo limpio **no separa** (AUC 0.430 < 0.55). El feature
  set actual de v2 (m30/m60/m90/rv_90s + macro + microprice
  hardcoded + tiempo) **no tiene signal predictivo** sobre Binance
  OHLCV en el horizonte de 90 s.
- v2 en producción (`v2_2026-04-23T20-06-38Z is_active=true`) está
  sirviendo predicciones aprendidas contra ground-truth invertido —
  su AUC reportado 0.664 es ruido sobre labels falsas.

**Recomendación accionable** (decisión de Hector):

1. **Despromover el activo** (`UPDATE research.models SET is_active =
   FALSE WHERE version = 'v2_2026-04-23T20-06-38Z'`). El modelo
   activa `shadow=false` en el TOML, pero su edge-threshold filter
   debería estar bloqueando entries dado que sus probas se calibran
   contra el mundo equivocado. Aún así, despromover es la opción
   conservadora.
2. **NO promover** ningún v2_* (incluido el clean) hasta repensar
   features. El bug de labels era el síntoma, pero curarlo no
   regenera el signal — hay que ingerir el libro Polymarket histórico
   o agregar features de microestructura nuevas (CVD 1 m, OFI, mlofi)
   antes de re-medir.
3. **Re-evaluar** todas las decisiones tomadas con métricas v2
   pre-25-abr. Las paper-trading sessions del modelo activo son
   también ruido.

Caso del audit: **fix técnico aplicado y validado, pero hipótesis v2
falsificada en serio.** El `.md` se mueve a `descartadas/` porque
el audit cierra; v2 mismo queda en
`en-desarrollo/last_90s_forecaster_v2_bbres.md` con el resultado
adverso registrado.

## Historial

### 2026-04-25 — auditoría
Ejecutada sobre `/btc-tendencia-data/polybot-agent.db`. 40.5 %
disagreement con Binance 1 m sobre el training set de v2.
`scripts/audit_polybot_groundtruth.py` reproducible, read-only.
Verdict POLYBOT SESGADO.

### 2026-04-25 — fix aplicado y v2 falsificado contra labels limpias
Re-derivado `open_price` / `close_price` desde Binance 1 m en
`_load_resolved_markets`. Re-train con labels limpias da
**AUC 0.430** (debajo de random) y bb_residual lift 0.000 pp.
Conclusión: el bug de labels enmascaraba ausencia de signal. Activo
v2 en producción aprendió relación invertida. Caso del audit
cerrado, `.md` movido a `descartadas/`.
