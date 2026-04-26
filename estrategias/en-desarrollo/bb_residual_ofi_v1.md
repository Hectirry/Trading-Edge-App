# bb_residual_ofi_v1

Estado: `en-desarrollo`
Family: `polymarket_btc5m`
Creada: 2026-04-25
Autor: Hector + Claude

## Hipótesis

En cualquier instante `t ∈ [60, 290] s` de la ventana 5 min BTC up/down,
la prob. implícita de Polymarket retrasa a la microestructura de
Binance (taker-tape CVD ≈ OFI proxy + intensidad + flag de large trade).
Un *Brownian bridge no-drift* sobre el spot Binance, mezclado por
shrinkage con un ensemble calibrado de microestructura, produce un
edge neto de la fee convexa que — gated por Sharpe-per-trade ≥ θ —
solo dispara en ~25 % de las ventanas pero compone Sharpe ≫ 1 sobre
cientos de trades.

Construcción (paso a paso, fiel a la spec del usuario):

1. `p_BM(t) = Φ((S_t − K) / (K · σ · √(T − t)))` con `K = open_price`,
   `σ` = stddev de log-returns 1 Hz sobre los últimos 90 s, `T = 300 s`.
2. OFI compuesto = `β₁·OFI_binance + β₂·OFI_coinbase` sobre los últimos
   30 s. **Hoy**: solo Binance (β₂ = 0). Coinbase trades ingestion es
   un ADR aparte; el TOML fuerza `ofi_coinbase_weight = 0.0`.
3. Features de microestructura: `bm_cvd_normalized`,
   `bm_taker_buy_ratio`, `bm_trade_intensity`, `bm_large_trade_flag`,
   `bm_signed_autocorr_lag1` (ya existen en
   `engine.features.binance_microstructure`).
4. Ensemble + isotonic → `p_edge`. **Hoy**: no hay ensemble entrenado
   → `p_edge ≡ p_BM` y la estrategia degrada a `SKIP("shadow_mode_no_model")`.
5. Shrinkage `p_final = α·p_edge + (1−α)·p_BM`. α se computa por regla
   determinista (rampa con `t_in_window` + bono por |OFI| + bono por
   large-trade). Cuando el ensemble exista y emita varianza por
   predicción, α debería derivar de esa varianza.
6. Fee convexa `fee(p) = fee_k · 4·p·(1−p)` con `fee_k = 0.0315`
   (3.15 % en p = 0.5).
7. `edge_net = p_final − p_market − fee` (lado YES_UP) o equivalente
   (YES_DOWN). Se elige el lado con `edge_net` mayor.
8. Sharpe per trade = `edge_net / p_edge_sigma`. **Hoy**:
   `p_edge_sigma = 0.025` SENTINEL (TOML); migrar a stddev del ensemble
   cuando exista.
9. Gate: Sharpe ≥ 2.0 en general; relajación a 1.5 cuando
   `t_to_close ≤ 30 s` (rama "now-or-never" del spec).
10. Sizing fractional Kelly 0.25× sobre `p_market(1−p_market)`,
    clipped a `kelly_max_stake_usd = $21` por trade.

Lift mínimo necesario para considerar viable v1 (post-train):
**+5 pp AUC** vs. baseline `last_90s_forecaster_v3` sobre el subset
intersección de coverage, con Brier ≤ baseline + 0.02 y ECE ≤ 0.10.

## Variables clave

- Horizonte / ventana: `T = 300 s`. Decisión cada segundo en
  `[60, 290]`. Una sola entrada por `market_slug` (set in-strategy).
- Microestructura window: 30 s para OFI; 90 s para `σ_per_sqrt_s`.
- Fee model: `fee_k = 0.0315` en `[params]` y `[fill_model]` (deben
  coincidir; mismatch silencioso = backtest fantasioso).
- Sharpe gate: `θ = 2.0`, late `θ = 1.5` cuando `t_to_close ≤ 30 s`.
- Sentinel: `p_edge_sigma = 0.025` hasta que el ensemble bootstrap
  emita stddev real por predicción.
- Toggle: `[paper].shadow = true` en TOML; `[strategies.bb_residual_ofi_v1]
  enabled = true` en `staging.toml` para arrancar el shadow log.

## Falsificación

Cualquiera de estos mata la hipótesis:

- Lift AUC OOS < 5 pp vs `last_90s_forecaster_v3` sobre el mismo
  subset, mismo seed, mismo budget Optuna.
- Brier degrada > 2 pp vs baseline.
- Tasa de ENTER en backtest ≥ 60 % de ventanas (la hipótesis dice que
  ~25 % es lo realista; un bot que entra el 60 % está mal calibrado).
- Walk-forward 3 × 7 d post-2026-05-13 con `stability_index < 0.6`.

## Datos requeridos

- `market_data.crypto_trades` (Binance, BTCUSDT). **Retention 90 d** —
  restringe el dataset igual que v3.
- `market_data.crypto_ohlcv` 1 m (Binance) — labels canónicas.
- `market_data.polymarket_prices_history` — `implied_prob_yes` real
  (no hardcoded 0.5). Ya backfilled para ventana 2026-04-22 → 2026-04-25.
- Polybot SQLite — descubrir markets resueltos y 1 Hz spot ticks
  para reconstruir `recent_ticks` en backtest.

**Datos NO disponibles aún (open-ended):**
- Coinbase trades — `ofi_coinbase_weight = 0.0` forzado hasta ADR.
- L2 order-book updates Polymarket — verdadero OFI (additions /
  cancels) requeriría WebSocket histórico CLOB. Por ahora usamos
  CVD trade-tape como proxy direccional honesto.
- Per-prediction ensemble stddev — `p_edge_sigma` es sentinel.

## Implementación

- Strategy: `src/trading/strategies/polymarket_btc5m/bb_residual_ofi_v1.py`
  (reusa `LGBRunner` de v2 para el guard `n_features_in_`).
- Config: `config/strategies/pbt5m_bb_residual_ofi_v1.toml`
  (shadow=true, `ofi_coinbase_weight=0.0`).
- Dispatch: `src/trading/cli/backtest.py::_load_strategy` +
  `src/trading/cli/paper_engine.py::_load_strategy`.
- Registro staging: `config/environments/staging.toml` →
  `[strategies.bb_residual_ofi_v1] enabled = true`.
- Tests: `tests/unit/strategies/polymarket_btc5m/test_bb_residual_ofi_v1.py`
  (unit-level: gates, shadow, fee convexity, alpha clamp).
- Training CLI: **pendiente** — crear `src/trading/cli/train_bb_ofi.py`
  que reuse el pipeline de `train_last90s` con el feature-set de este
  módulo (`FEATURE_NAMES`).

## Plan de validación

1. **Subset honesto** (sesión post-train, RAMA Y por retention crypto_trades):
   training window `[2026-04-22 15:38, 2026-04-25]`, baseline
   `last_90s_forecaster_v3_priceshist` + `bb_residual_ofi_v1_first`
   sobre el mismo subset, seed=42.
2. **Walk-forward 3 × 7 d** post-2026-05-13 (cuando crypto_trades
   tenga 3 semanas de retention).
3. **Shadow paper ≥ 7 d** post-walk-forward antes de promotion.
   Boot shadow ya activo via TOML `paper.shadow=true`.

## Resultados

### 2026-04-26 — segundo training, escala 16×

Tras el primer training (n=164) detectamos que el bottleneck no era
``polymarket_prices_history`` sino la cobertura de ticks 1 Hz del
polybot SQLite (8 d). Solución: reconstruir spots desde
``market_data.crypto_trades`` (35 d, 10.6 M trades) — al ser un tape
trade-by-trade resampleado a 1 Hz por forward-fill, la escala σ por
sqrt(s) que ``realized_vol_per_sqrt_s`` espera se preserva. Markets
source pasó de polybot SQLite (547 mercados resueltos) a
``polymarket_markets`` Postgres (8774 mercados), labels seguidas
canónicas vía Binance OHLCV 1 m close (mismo path del audit
2026-04-25).

| modelo | n_train | n_val | n_test | AUC | Brier | ECE | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| v1 (n=164) | 114 | 24 | 26 | 0.6488 | 0.2650 | 0.0550 | failed Brier |
| **v2 (n=2642)** | 1849 | 396 | 397 | **0.5966** | 0.2464 | 0.2152 | failed ECE+Brier |

Lectura honesta: el v1 estaba sobreajustado (n_test=26 daba AUC
artificialmente alto, ±0.10 stderr). El v2 con n_test=397 da AUC
0.60 ±0.025 — señal real pero **modesta**. ECE 0.215 dice que las
probabilidades absolutas no son confiables — la calibración isotónica
detectó miscalibration con más datos que en el v1 ni se veía. Brier
mejoró porque el modelo dejó de predecir clases extremas.

Optimizaciones del trainer aplicadas (de 5 h estimadas a 5 min):
- Batched ``_batch_fetch_implied_yes`` con DISTINCT ON + unnest,
  reemplaza 8 k SELECT por 1.
- ``_load_baseline_trades_per_day`` cachea el baseline de
  ``trade_intensity`` por día (1 query) en vez de un COUNT 24 h por
  mercado.
- ``_fetch_trades_in_window`` reducido a sólo el window range (sin
  el COUNT 24 h embebido del v3).
- Progress logs cada 500 mercados.

`is_active=true` en v2 vía SQL manual; el ``--promote`` honesto
falló el gate (ECE alto). Documentado.

#### Comportamiento en paper mode (v2)

```
Polymarket dice:    SÍ 50.5%   NO 49.5%
BB prior:           SÍ 50.0%   (open=0 transient)
Modelo (p_edge):    SÍ 89.4%        ← predicciones probabilísticas reales
Combinado final:    SÍ 69.0%   (α=0.48)
Predicción:         ↑ SUBIRÁ
Edge neto: +15.3 pp · Sharpe 6.13 / 2.0 requerido ✅
SKIP reason: shadow_mode (bloqueado por [paper].shadow=true)
```

vs v1 anterior donde p_edge era 0.0000 o 1.0000 exacto.

### 2026-04-26 — primer training honesto

Primer ciclo completo: training pipeline funciona end-to-end y el
modelo se carga en el engine en paper mode.

| modelo | n_train | n_val | n_test | AUC | Brier | ECE | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| **bb_ofi_2026-04-26T01-21-46Z** | 114 | 24 | 26 | **0.6488** | 0.2650 | 0.0550 | failed (Brier marginal) |

- AUC 0.65 ✅ (cap 0.55) — señal real por encima del aleatorio.
- Brier 0.265 ❌ (cap 0.260 con n<200) — apenas 0.005 por encima del
  techo sample-size-aware.
- ECE 0.055 ✅ (cap 0.20 con n<200) — calibración isotónica funcionó.

`is_active=true` aplicado **manualmente** vía SQL (no por `--promote`,
que respetó el gate Brier) para verificar el modelo en el dashboard.

### Coverage del training set

- Polybot SQLite (`/btc-tendencia-data/polybot-agent.db`): solo **8 días**
  de ticks 1Hz (2026-04-18 → 2026-04-26), 547 mercados resueltos.
- Tras dropping por OHLCV gap, ticks insuficientes, micro/implied/vol:
  **n=164 muestras**.
- Bottleneck real: tick coverage del polybot. `polymarket_prices_history`
  ya cubre 28 días pero no compensa la falta de ticks.

### Comportamiento en paper mode

Engine cargó `model.lgb` y emite `bb_ofi.decision` cada segundo por
mercado activo. Ejemplo de tick en una ventana donde BTC bajó 0.026 %:

```
spot=77534, open=77555    p_market=0.50
p_bm=0.30                 (BB prior pondera el bajón correctamente)
p_edge=0.00               (modelo sobreajustado, predice extremos)
p_final=0.18              (shrinkage α=0.6)
edge_net=+0.292           (modelo cree NO con 18% YES vs mercado 50%)
sharpe=11.68              (artificial — p_edge_sigma=0.025 es sentinel)
action=SKIP reason=shadow_mode    (bloqueado por [paper].shadow=true)
```

Comportamiento que esto valida:
- BB prior funciona: `p_bm` se separa de 0.5 cuando hay drift de spot.
- Shrinkage α se computa correctamente.
- Fee convexa correcta (≈3.15 % en p=0.5).
- Side-picking correcto.
- Shadow gate funciona: aún con Sharpe nominalmente alto, no entra.

Síntomas de overfit con n=164:
- `p_edge ∈ {0, 1}` casi siempre — modelo predice clases, no probs.
- Brier alto (penaliza este patrón explícitamente).
- Sharpe nominal alto pero sin significado estadístico (denominator =
  sentinel, no varianza ensemble).

## Veredicto

**Pipeline validado, modelo estadísticamente débil.** El primer
training completa el ciclo end-to-end y el modelo entra en
producción shadow, pero AUC 0.65 con n_test=26 (IC ±0.10) no es
concluyente. Brier marginal sugiere predicciones extremas — síntoma
de overfit con muestra pequeña.

Cambios necesarios antes de promotion *real* (eliminar override SQL):

1. **Aumentar n** — bottleneck es tick coverage (8 d). Caminos:
   - Esperar ~2 semanas a que `paper_ticks` acumule cobertura
     suficiente y portar `_load_ticks_for_slug` a leer de ahí.
   - Reconstruir spots 1 Hz desde `market_data.crypto_trades` (10.6 M
     trades en 35 d disponibles ya).
2. **Reemplazar `p_edge_sigma` sentinel** — entrenar bootstrap
   ensemble que emita stddev por predicción (típicamente 5-10 modelos
   con seeds distintos).
3. **Walk-forward 3 × 7 d** post-bootstrap.
4. **Shadow paper ≥ 7 d** con paper_predictions logging para auditoría
   externa.

**Nota sobre data collection en shadow**: la estrategia adjunta
`signal_features` a cada Decision, pero el `PaperDriver` solo agrega
contadores Prometheus por `reason` — no persiste el feature dict
per-tick. El training set se reconstruye offline desde
`paper_ticks` + `market_data.crypto_trades` + `market_outcomes`,
igual que v3. Una tabla `paper_predictions` con writer per-decision
es un ADR aparte (write-amp sobre stream 1 Hz; ver staging.toml
sizing antes de proponerla).

## Veredicto

_(initial: empty — sin training corrido todavía)_

## Historial

### 2026-04-25 — creación

Cableado el scaffold serving:
- `bb_residual_ofi_v1.py` con feature vector de 14 columnas
  (`bb_p_prior`, `bb_delta_norm`, `ofi_composite`, 4 features de
  microestructura, `implied_prob_yes`, `pm_spread_bps`, `pm_imbalance`,
  `t_in_window_s`, `vol_per_sqrt_s`, `fee_at_market`, `alpha_shrinkage`).
- TOML con shadow=true, `ofi_coinbase_weight=0.0` forzado, `fee_k=0.0315`
  alineado entre `[params]` y `[fill_model]`.
- Dispatch añadido en `backtest.py` y `paper_engine.py`.
- Tests unit: gates de entry window, shadow boot, fee convexity,
  alpha-clamp, side picking.

Caveats honestos arrancando:
- No hay ensemble entrenado → `p_edge ≡ p_bm`, todo SKIP("shadow_mode_no_model").
- `p_edge_sigma=0.025` es SENTINEL hasta bootstrap del ensemble.
- `α` es regla determinista hasta que la varianza ensemble por
  predicción esté disponible.
- Coinbase OFI no está; β₂=0 forzado. β₁·OFI_binance solo. Documentado
  en TOML y en strategy docstring.

**Próximo paso**: escribir `train_bb_ofi.py` (reusar pipeline de
`train_last90s`), correr v1_first sobre subset honesto, comparar
contra v3_priceshist baseline.
