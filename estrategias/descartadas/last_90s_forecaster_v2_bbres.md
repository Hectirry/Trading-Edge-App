# last_90s_forecaster_v2_bbres

Estado: `en-desarrollo` (falsificada — ver Resultados)
Family: `polymarket_btc5m`
Creada: 2026-04-25
Autor: Hector + Claude

## Hipótesis

Agregar el prior Brownian-bridge al vector de v2 — como 4 features
derivadas (`bb_p_prior`, `bb_model_vs_prior`, `bb_market_vs_prior`,
`bb_edge_vs_market`) — mejora el AUC OOS en ≥ 0.5 pp manteniendo
Brier ≤ 0.245 y ECE ≤ 0.05, sin cambiar arquitectura. La intuición es
que `bb_market_vs_prior = implied_prob_yes − p_BM` captura lead-lag
Binance → Polymarket sin ingerir order-flow.

## Variables clave

- Horizonte / ventana: heredado de v2 — entrada T-90s (`as_of = open + 210`),
  resolución T=300 s.
- Señal de dirección: `edge = micro_prob_v2 − implied_prob_yes` con
  `|edge| ≥ edge_threshold = 0.02`.
- Features añadidos al final del vector v2 (orden bb-tail = preserva
  serving de modelos sin bb):
  ```
  bb_p_prior, bb_model_vs_prior, bb_market_vs_prior, bb_edge_vs_market
  ```
- Sizing: heredado v2 — Kelly fraccionado, stake 5 USD.
- Toggle: `[params].use_bb_residual_features` (default `false`).

## Falsificación

Cualquiera de:

- AUC OOS de v2_bbres < AUC de v2_clean entrenado mismo seed/dataset
  + 0.5 pp.
- Brier_test > 0.245 con n_test ≥ 100.
- ECE_val > 0.05 con n_val ≥ 100.
- Walk-forward stability_index < 0.6 (si la CLI lo emite).

## Parámetros provisionales

```
[params]
use_bb_residual_features = true
bb_residual_T_seconds    = 300.0
```

Resto inalterado vs `pbt5m_last_90s_forecaster_v2.toml` activo.

## Datos requeridos

Todo ya existe. Idéntico al pipeline v2:

- `market_data.crypto_ohlcv 5m` BTCUSDT (Binance) — desde 2025-10.
- Polybot SQLite `/btc-tendencia-data/polybot-agent.db` (ticks +
  trades resueltos).
- No necesita `market_data.paper_ticks` ni libro Polymarket histórico
  (training usa defaults neutros — limitación pre-existente del v2).

## Implementación

Aplicada en commit pendiente (sesión 2026-04-25):

- Módulo feature: `src/trading/engine/features/bb_residual.py`
  - `brownian_bridge_prob(spot, open_, t_in_window_s, vol_per_sqrt_s, T=300, eps=1e-6)`
    pure, sin scipy (math.erf).
  - `bb_residual_features(ctx)` convierte `ctx.vol_ewma` annualised →
    per-sqrt(s) (`/sqrt(31_536_000)`) antes de delegar.
- Vector v2: `src/trading/strategies/polymarket_btc5m/_v2_features.py`
  añade fields `open_price`, `t_in_window_s`, `bb_T_seconds` a
  `V2FeatureInputs`; `build_vector(inp, *, include_bb_residual=False)`
  appendea las 4 claves al final cuando se activa.
- Strategy serving:
  `src/trading/strategies/polymarket_btc5m/last_90s_forecaster_v2.py`
  lee `use_bb_residual_features` del TOML, pasa los nuevos fields desde
  `ctx`, y `LGBRunner.predict_proba` ahora valida
  `len(x) == booster.num_feature()` con error explícito (anti-skew).
- Training: `src/trading/cli/train_last90s.py` añade
  `--include-bb-residual` (versión `v2_bbres_<ts>`), `--seed` (TPESampler
  con seed fijo para repro). `build_samples` propaga `open_price`/
  `t_in_window_s` y la flag.
- Walk-forward: `src/trading/cli/walk_forward.py` propaga la flag por
  `_eval_last_90s_v2_fold`.
- TOML: `config/strategies/pbt5m_last_90s_forecaster_v2.toml` añade
  `use_bb_residual_features = false` (retro-compat, no rompe el
  modelo activo de 21 features).
- Tests:
  - `tests/unit/engine/features/test_bb_residual.py` (14 cases, 100 %
    pass).
  - `tests/unit/strategies/test_last_90s_forecaster_v2.py` actualizado
    para validar invariante `len(vec_bb)=25, vec_bb[:21]==vec_base`.

## Plan de validación

1. Backtest inicial deferred — primero medir lift en training.
2. Walk-forward 3×7 d sólo si lift ≥ 0.5 pp en split único.
3. Paper trading mínimo 7 d antes de promoción.

## Resultados (2026-04-25 — falsificación)

Mismo dataset, mismo split, mismo seed (=42), mismo budget (50 trials,
900 s). Ambos modelos escritos en `research.models` con
`is_active = false`.

| modelo                              | AUC    | Brier  | ECE    | n_features |
|-------------------------------------|--------|--------|--------|------------|
| v2_2026-04-23T20-06-38Z (activo)    | 0.6644 | 0.2527 | 0.1084 | 21         |
| v2_2026-04-25T04-32-11Z (clean)     | 0.5342 | 0.2336 | 0.1149 | 21         |
| v2_bbres_2026-04-25T04-32-29Z       | 0.5342 | 0.2336 | 0.1149 | 25         |

**Lift bb_residual vs v2_clean = 0.000 pp en AUC, Brier y ECE.** Ambos
modelos coinciden hasta el último dígito porque, con
`include_bb_residual=True`:

1. `bb_model_vs_prior ≡ 0` por construcción
   (`model_prob = p_prior` en train + serve para no introducir
   skew de vol).
2. `bb_market_vs_prior = 0.5 − p_prior` y
   `bb_edge_vs_market = p_prior − 0.5` (training hardcodea
   `implied_prob_yes = 0.5`) son transformaciones lineales
   deterministas de `bb_p_prior`.
3. `bb_p_prior` mismo es función de spot/open/rv/t, todos ya
   codificados implícitamente en m30/m60/m90 + rv_90s.

LightGBM con `TPESampler(seed=42)` encuentra exactamente el mismo
óptimo en ambas corridas. Walk-forward se omite — repetiría la misma
identidad numérica en cada fold sin información adicional.

Drift v2_clean vs activo (AUC −13 pp, Brier −1.9 pp): es ruido
de Optuna + 2.6× más datos (507 vs 134 train+val+test) + budget
distinto (50 vs 200 trials). Ambos pasan la cap Brier sample-size-aware.

### Veredicto

**Falsificada.** La construcción spec'd (con
`implied_prob_yes` hardcodeado en training) garantiza lift = 0 en
training. Para que `bb_market_vs_prior` carry signal real, la pipeline
de training tiene que ingerir el libro Polymarket histórico —
trabajo de mayor scope, no candidato a iteración rápida. **No
promover.** No iterar bb_residual hasta cerrar esa deuda.

## Historial

### 2026-04-25 — creación + falsificación
Implementada feature `bb_residual` y wiring training/serving en una
sesión. Con `--seed 42 --optuna-trials 50 --time-budget-s 900` ambas
versiones (con y sin bb) producen métricas idénticas. La construcción
del feature en training colapsa a una transformación lineal de
features ya presentes, condicionada al hardcodeo de
`implied_prob_yes = 0.5`. Caso archivable como falsificación
explícita; recomendación es saltar a otra hipótesis o auditar la
pipeline de training para ingerir libro PM real.

### 2026-04-25 — re-medición sobre labels limpias (POLYBOT_SESGADO fix)

Tras el audit `_audit_polybot_groundtruth.md` que mostró 40.5 % de
labels invertidas en el training set v2, se aplicó fix a
`_load_resolved_markets` para re-derivar `open_price` / `close_price`
desde Binance OHLCV 1 m. Re-train con etiquetas limpias, mismo seed
42, mismo budget:

| modelo                            | n_train | AUC   | Brier | ECE  |
|-----------------------------------|---:|---:|---:|---:|
| `v2_2026-04-25T04-57-43Z` (clean) | 138 | 0.430 | 0.277 | 0.168 |
| `v2_bbres_2026-04-25T04-58-09Z`   | 138 | 0.430 | 0.277 | 0.168 |

**Lift bb_residual sobre labels honestas = 0.000 pp.** Misma causa
que el ciclo previo: con `implied_prob_yes` hardcodeado a 0.5 en
training, los 4 features bb_* colapsan a transformaciones de uno solo
(`bb_p_prior`) más una constante. Doble falsificación: la
construcción es defectuosa contra cualquier ground-truth, no sólo
la sesgada.

**Hipótesis cerrada como descartada.** Para re-abrirla habría que
ingerir el libro Polymarket histórico al training (escapa al scope
de este flujo) y/o reformular `model_prob_yes` para que aporte
información independiente del prior. El archivo `.md` se mueve a
`descartadas/` cuando el siguiente ciclo confirme dirección.
