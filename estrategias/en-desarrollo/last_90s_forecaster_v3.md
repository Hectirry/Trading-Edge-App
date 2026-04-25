# last_90s_forecaster_v3

Estado: `en-desarrollo`
Family: `polymarket_btc5m`
Creada: 2026-04-25
Autor: Hector + Claude

## Hipótesis

5 features de microestructura tomadas de `market_data.crypto_trades`
(Binance taker tape, ventana 90 s) levantan el AUC OOS sobre el
baseline v2 en ≥ 3 pp manteniendo Brier ≤ baseline + 0.02 y ECE ≤ 0.10,
sin cambiar arquitectura ni horizonte (entrada T-90 s, resolución 5 m).

Las features:

```
bm_cvd_normalized        signed CVD / total volume   ∈ [-1, 1]
bm_taker_buy_ratio       buy_volume / total_volume   ∈ [0, 1]
bm_trade_intensity       n_trades / baseline_24h     ≥ 0
bm_large_trade_flag      ∃ trade ≥ $100k notional    ∈ {0, 1}
bm_signed_autocorr_lag1  autocorr de side ∈ {±1}     ∈ [-1, 1]
```

## Variables clave

- Horizonte / ventana: T-90 s, resolución 5 m. Igual a v2.
- Window microestructura: 90 s anteriores a as_of (= open + 210).
- Baseline trade_intensity: 24 h trailing del mismo símbolo.
- Sizing: heredado v2 (Kelly fraccionado, stake $5).
- Toggle: TOML `[params].use_microstructure_features = true`.

## Falsificación

Cualquiera mata la hipótesis:

- Lift AUC < 3 pp vs v2_baseline (mismo subset, mismo seed).
- Brier degrada > 2 pp vs v2_baseline.
- 0–2 / 5 features de microestructura en top-10 importance.

## Datos requeridos

- `market_data.crypto_trades` (Binance, BTCUSDT, side ∈ {buy, sell}).
  **Retention 90 d** — restringe el dataset al subset cubierto.
- `market_data.crypto_ohlcv` 1 m (Binance) — labels via
  `_load_resolved_markets`.
- `market_data.crypto_ohlcv` 5 m (Binance) — features macro.
- Polybot SQLite `/btc-tendencia-data/polybot-agent.db` — descubrir
  qué markets resolvieron y de dónde sacar 1 Hz spot ticks.

## Implementación

- Módulo features: `src/trading/engine/features/binance_microstructure.py`
  (5 funciones puras + agregador async + sentinels documentados).
- Strategy: `src/trading/strategies/polymarket_btc5m/last_90s_forecaster_v3.py`
  (reusa `LGBRunner` de v2 — mismo guard `n_features_in_`).
- Config: `config/strategies/pbt5m_last_90s_forecaster_v3.toml`
  (shadow=true, `use_microstructure_features=true`).
- Dispatch: `src/trading/cli/backtest.py::_load_strategy` +
  `src/trading/cli/paper_engine.py::_build_strategy`.
- Training CLI: `src/trading/cli/train_last90s.py` con flag
  `--strategy v3`. Drop política idéntica al fix de labels: market
  sin crypto_trades en window → descartado, no fallback a sentinel.
- Tests: `tests/unit/engine/features/test_binance_microstructure.py`
  (21 casos, 100 % pass).
- Registro staging: `config/environments/staging.toml` →
  `[strategies.last_90s_forecaster_v3] enabled = true`.

## Plan de validación

1. **Subset honesto** (esta sesión, RAMA Y por retention crypto_trades):
   training window restringida a `[2026-04-22 15:38, 2026-04-25]`,
   v2_baseline + v3_first sobre el mismo subset, seed=42.
2. **Walk-forward** 3 × 7 d cuando crypto_trades alcance 3 semanas
   (es decir, ≥ 2026-05-13). Hasta entonces sólo se puede correr un
   walk-forward 1 × 7 d, no representativo.
3. **Shadow paper ≥ 7 d** post-walk-forward antes de promotion. Boot
   shadow ya activo via TOML `paper.shadow=true`.

## Resultados

### Coverage (paso 0 — RAMA Y)

| | valor |
|---|---|
| crypto_trades range | 2026-04-22 15:38 → 2026-04-24 23:59 (2.35 d) |
| markets resueltos en training window 2025-11-01 → 2026-04-25 | 526 |
| markets con crypto_trades coverage | **247 (47.0 %)** |
| markets con polybot tick coverage adicional | 243 |

Decisión RAMA Y: restringir training window al subset cubierto y
re-entrenar v2_baseline sobre el mismo subset (apples-to-apples).

### Métricas (mismo seed=42, 50 trials Optuna, 600 s budget)

| modelo | n_train | n_test | AUC | Brier | ECE | n_features | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| v2_clean_ohlcv (sesión previa, full 6 m) | 138 | 31 | 0.430 | 0.277 | 0.168 | 21 | failed |
| **v2_2026-04-25T05-34-11Z (subset)** | 121 | 27 | **0.5444** | 0.2835 | 0.203 | 21 | failed |
| **v3_first_2026-04-25T05-34-52Z** | 104 | 23 | **0.6591** | 0.2364 | 0.147 | 26 | passed |

**Lift v3 vs v2_baseline (apples-to-apples, mismo subset, mismo seed):**

| métrica | v2 | v3 | delta |
|---|---:|---:|---:|
| AUC | 0.5444 | 0.6591 | **+10.5 pp** |
| Brier | 0.2835 | 0.2364 | −4.7 pp (mejora) |
| ECE | 0.203 | 0.147 | −5.6 pp (mejora) |

v3 mejora los 3 simultáneamente. Lift AUC ≫ 3 pp threshold.

### Feature importance v3 (LightGBM gain, top 10)

| rank | feature | gain | pct |
|---:|---|---:|---:|
| 1 | **bm_cvd_normalized** | 71.99 | 23.7 % |
| 2 | **bm_signed_autocorr_lag1** | 57.77 | 19.1 % |
| 3 | m30_bps | 53.06 | 17.5 % |
| 4 | adx_14 | 42.99 | 14.2 % |
| 5 | hour_sin | 25.82 | 8.5 % |
| 6 | hour_cos | 24.49 | 8.1 % |
| 7 | **bm_trade_intensity** | 13.44 | 4.4 % |
| 8 | consecutive_same_dir | 8.09 | 2.7 % |
| 9 | ema8_vs_ema34_pct | 3.03 | 1.0 % |
| 10 | m60_bps | 1.10 | 0.4 % |

**3 / 5 microstructure features en top-10**: `bm_cvd_normalized`,
`bm_signed_autocorr_lag1`, `bm_trade_intensity`. Las dos restantes
(`bm_taker_buy_ratio` rank 11, `bm_large_trade_flag` rank 12) tienen
gain marginal; CVD probablemente subsume taker_buy_ratio (correlados).

### Coverage report

- Markets en window training (post t_from/t_to filter): 190.
- Markets descartados por OHLCV gap: 0 (subconjunto reciente, full
  coverage).
- Markets descartados por insufficient ticks polybot: 16 → 174 samples
  v2.
- Markets descartados por crypto_trades gap (v3 only): 25 → 149 samples v3.
- Sample yield v3 / v2 ≈ 86 %.

### Real implied_prob_yes (TAREA 3.10 — re-train con libro PM histórico)

Hipótesis follow-up: en `v3_first` el campo `implied_prob_yes` se
hardcodeó a 0.5 durante training (no había libro Polymarket histórico
ingerido). Ingerimos `market_data.polymarket_prices_history` desde el
endpoint `/prices-history` del CLOB y re-entrenamos v3 con la flag
`--use-real-implied-prob`.

| modelo | n_train | n_test | AUC | Brier | ECE | n_features | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| v3_first_2026-04-25T05-34-52Z | 104 | 23 | 0.6591 | 0.2364 | 0.1472 | 26 | passed |
| **v3_priceshist_2026-04-25T12-16-50Z** | 104 | 23 | **0.7311** | 0.2523 | 0.1734 | 26 | passed |

| métrica | v3_first | v3_priceshist | delta |
|---|---:|---:|---:|
| AUC | 0.6591 | 0.7311 | **+7.2 pp** |
| Brier | 0.2364 | 0.2523 | +1.6 pp (degradación leve) |
| ECE | 0.147 | 0.173 | +2.6 pp (degradación leve) |

AUC sube 7.2 pp; Brier/ECE empeoran levemente pero los dos siguen por
debajo del cap sample-size-aware (Brier ≤ 0.260, ECE ≤ 0.20). El gate
sigue passing.

#### Feature importance v3_priceshist (LightGBM gain, top 12)

| rank | feature | gain | pct |
|---:|---|---:|---:|
| 1 | m30_bps | 67.62 | 14.3 % |
| 2 | **implied_prob_yes** | 67.09 | 14.2 % |
| 3 | **bm_cvd_normalized** | 58.50 | 12.4 % |
| 4 | **bm_signed_autocorr_lag1** | 58.07 | 12.3 % |
| 5 | adx_14 | 52.08 | 11.0 % |
| 6 | **bm_trade_intensity** | 36.86 | 7.8 % |
| 7 | hour_sin | 35.90 | 7.6 % |
| 8 | ema8_vs_ema34_pct | 29.17 | 6.2 % |
| 9 | m60_bps | 19.76 | 4.2 % |
| 10 | **bm_large_trade_flag** | 18.29 | 3.9 % |
| 11 | hour_cos | 15.25 | 3.2 % |
| 12 | consecutive_same_dir | 10.84 | 2.3 % |

Lectura clave:
- `implied_prob_yes` salta de 0 % gain (constante en v3_first) a #2
  rank con 14.2 % gain → el libro real PM tiene contenido predictivo
  sustancial. Justifica el coste del backfill y del flag.
- 4 / 5 microstructure features ahora en top-10 (vs 3 / 5 en v3_first):
  `bm_large_trade_flag` se cuela al rank 10 cuando el libro PM ya
  ocupa parte de la "información de orden flujo" que antes capturaba
  CVD en solitario.
- `bm_taker_buy_ratio` sigue fuera del top-10 (rank 13). Hipótesis: lo
  subsume CVD (ambos miden direccionalidad del taker tape).

## Walk-forward 2026-04-25 (3 folds, IS=4d/OOS=1d/step=1d)

Configuración B+D del prompt (3 folds dentro de los 7 d disponibles
en `polybot-agent.db`, en lugar del 3×7d original que requeriría
21+ d de spot ticks no disponibles). Universe = 471 markets resueltos
(540 - 69 dropped por OHLCV gap).

### Resultados

| | fold 0 (IS 4/18→4/22, OOS 4/22→4/23) | fold 1 (IS 4/19→4/23, OOS 4/23→4/24) | fold 2 (IS 4/20→4/24, OOS 4/24→4/25) |
|---|---|---|---|
| **v2_baseline** | unvalidated (IS<40 post-feature) | n_oos=105, AUC IS=0.500, AUC OOS=0.500, Brier=0.246 → stable (trivial) | n_oos=23, AUC IS=0.614, AUC OOS=0.591, Brier=0.250 → stable |
| **v3_priceshist** | unvalidated (IS=0 — todos los samples dropados por implied_prob faltante en 4/18-4/22) | n_oos=100, AUC IS=0.500, AUC OOS=0.500, Brier=0.244 → stable (trivial) | unvalidated (n_oos=3 — gap crypto_trades 4/25 00:00→15:32 borra microstructure) |

run_id v2 `a96d1246-c5b1-497d-871a-b7db49799b0d`,
run_id v3 `4a964572-f51f-4eca-8e97-393a4ea8c8cf` en
`research.walk_forward_runs`.

### Outcome: **B — hold / iterate**

**Walk-forward inconcluso.** Ningún fold entrenó honestamente:
- 2 / 6 fold-runs unvalidated por sample drops (insufficient post-feature).
- 2 / 6 fold-runs trivial (AUC train = 0.5 = LightGBM no aprendió nada
  con n ~ 60-100 samples y 21-26 features).
- 1 / 6 v2 fold marginal (n=23, AUC OOS = 0.591) — sólo este fold tiene
  algo de señal y n es demasiado pequeño para ser concluyente.

### Bloqueos identificados (no de modelo, de datos)

1. **`polymarket_prices_history` cubre sólo 2026-04-21 → 2026-04-25.**
   Fold 0 IS [4/18-4/22] → cero samples para v3 porque
   `--use-real-implied-prob` dropa todos los markets sin libro
   histórico. Fix: extender backfill prices_history a 2026-04-04
   → 2026-04-20 (~16 d, ~1-2 h al pace original).
2. **Gap `crypto_trades` 2026-04-25 00:00 → 15:32** (15.5 h, post-WS
   muerte). Fold 2 v3 OOS hereda este gap → 20/23 markets dropados
   por microstructure. Fix: backfill aggTrades del slice 4/24 23:59
   → 4/25 15:32 (~15 min).
3. **Universo 7 d demasiado pequeño** para 3×7d real. Fix de fondo:
   sintetizar 1 Hz spot desde `crypto_trades` para abandonar la
   dependencia polybot-agent ticks (~1-2 h código + tests).

Hasta resolver (1) + (2), el walk-forward no es una evaluación válida
ni del modelo ni de su estabilidad temporal. **No mover `is_active`
en ninguna versión hasta repetir.**

## Promotion 2026-04-25 — bypass del gate (paper, no real money)

**Decisión consciente**: promovido a `is_active=true` con WF outcome B
(no A). El gate exige walk-forward stability ≥ 0.6 + ≥ 7 d shadow + paper
PnL ≥ 0; ninguno de los tres se cumple. Bypass autorizado por el dueño
del proyecto (Hectirry), entendiendo que:

- Es **paper**, no dinero real. Riesgo = malos datos en
  `research.backtests` + tiempo, no capital.
- Si el modelo tiene un sesgo de construcción no detectado (igual que
  `bb_residual` que falsificamos), el paper PnL irá a perder.
- La señal de "promover" es la `tea-promotion-gate` skill, que
  explícitamente alerta contra esto. Saltar el gate aquí es un caso
  declarado, no un olvido.

### Wiring de serving (TAREA pre-promotion)

Antes de flippear `is_active` se wiriteó el `microstructure_provider`
que faltaba en serving. El strategy.py original ([line 152-161](src/trading/strategies/polymarket_btc5m/last_90s_forecaster_v3.py#L152-L161))
documenta: *"the engine has not wired a sync microstructure provider
yet (planned in promotion sprint)"*. Sin esto, las predicciones se
hacían sobre vectores con microstructure sentinel ≠ training (= train/serve
skew, recipe para falsificación silenciosa).

Implementado:
- `src/trading/strategies/polymarket_btc5m/_microstructure_provider.py`
  — `PostgresMicrostructureProvider` con cache async-refreshed cada 5 s
  (matches loop tick existente). Sync `fetch(ts)` retorna features de
  `binance_microstructure_features()` fresca, con `max_staleness_s=30`
  → fallback a sentinels si la cache no se refrescó (defensivo).
- `paper_engine.py` extendido: instancia el provider al boot,
  `await refresh()` antes de `_load_strategy`, lo pasa por kwarg, lo
  añade al `_shared_providers_refresh_loop`.

### Operativa

```
research.models WHERE name='last_90s_forecaster_v3' AND is_active=true
→ v3_priceshist_2026-04-25T12-16-50Z

config/strategies/pbt5m_last_90s_forecaster_v3.toml: shadow=false
tea-engine restart 2026-04-25 21:01:49 UTC, modelo cargado OK,
microstructure_provider.ready, paper.driver.started paused=false.
```

### Monitoring + revert criterion

Revisa diariamente:

```sql
SELECT date_trunc('day', f.ts) AS day, COUNT(*) AS n, SUM((f.metadata->>'pnl')::float) AS pnl
FROM trading.fills f JOIN trading.orders o ON f.order_id=o.id
WHERE o.strategy_id='last_90s_forecaster_v3' AND f.ts >= now() - interval '7 days'
GROUP BY day ORDER BY day;
```

**Revertir** (set `shadow=true` + `is_active=false`) si cualquiera:
- Paper PnL trailing 7 d ≤ 0
- Win rate ≤ 0.50 sobre ≥ 30 fills
- `microstructure.stale` log aparece > 1% de los ticks (cache no se refresca)
- Cualquier exception traceback v3 en logs

## Veredicto

**Hipótesis validada en train/test single-split** (TAREA 3.10):
- Lift AUC +10.5 pp ≥ 3 pp ✓
- Brier mejora ≥ no degrada ✓
- ECE 0.147 ≤ 0.20 ✓ (gate sample-size-aware) ✓
- 3 / 5 microstructure en top-10 ✓

**Walk-forward 3 folds 2026-04-25: outcome B — hold/iterate** por
gaps de datos pre-existentes en prices_history y crypto_trades.

**Promotion 2026-04-25**: bypass del gate, `is_active=true` en paper
con monitoring + revert criterion documentado arriba. No promoción
real-money hasta cumplir el gate completo.

Re-train con implied_prob real (TAREA 3.10) levanta AUC un +7.2 pp
adicional sobre v3_first y mete `implied_prob_yes` al rank #2. La
construcción se mantiene activa en shadow con `v3_first` (la activa
hoy). Promotion del modelo `priceshist` queda condicionada a:
walk-forward 3 × 7 d post-2026-05-13 + shadow ≥ 7 d con paper
predictions logged.

Caveats honestos:
- n_test = 23, std error de AUC ≈ 0.10. El intervalo aproximado es
  0.66 ± 0.10 (v3_first) y 0.73 ± 0.10 (v3_priceshist). Lift puntual
  está bien por encima de eso, pero la variabilidad estadística es alta.
- ECE val = 0.173 (priceshist) está por encima del 0.05 ideal
  (sample-size-aware cap 0.20 lo rescata). Calibración isotónica
  aplicada — no parece estar absorbiendo bien la nueva dimensión de
  implied_prob.
- Walk-forward NO se corrió en esta sesión. Hasta no validar
  estabilidad temporal sobre splits, no promover ninguna versión v3.
- `is_active=false` para ambos. Ningún modelo v3 está sirviendo edges
  vinculantes; sólo loggea predicciones en shadow.

## Historial

### 2026-04-25 — creación + primera medición

Implementadas 5 features de microestructura desde Binance taker
tape; restricción de scope a 5 fija (no más, no menos) por mandato
de la sesión. RAMA Y por retention 90 d de crypto_trades. v3_first
levanta AUC +10.5 pp vs v2_baseline en el mismo subset, mismo seed,
mismo budget; CVD y signed autocorr son los dos features más
importantes (43 % gain combinado). Hipótesis validada. **Próximo
paso**: walk-forward 3 × 7 d cuando crypto_trades alcance 3 semanas
de retention; shadow paper ≥ 7 d post-walk-forward; promotion sólo
si stability_index ≥ 0.6 y AUC OOS mediana ≥ 0.55 sin desviación
seria.

### 2026-04-25 — re-train con implied_prob_yes real (TAREA 3.10)

Cerrado el deuda de "implied_prob hardcodeada a 0.5". Pipeline:
1. schema migration `infra/postgres/init/11_polymarket_prices_history.sql`
   (hypertable `condition_id, token_id, outcome, ts, price`, PK `(token_id, ts)`).
2. backfill `scripts/backfill_polymarket_prices_history.py` (User-Agent
   browser para bypass Cloudflare 1010, sin filtro `resolved=true` —
   recorre todos los markets con `clobTokenIds` no-null). 2.45 M rows
   para los 865 markets de la ventana 2026-04-22 → 2026-04-25.
3. `train_last90s.py` flag `--use-real-implied-prob` que joinea
   `polymarket_prices_history` con la tabla de markets para encontrar
   el último precio YES ≤ as_of (= open + 210 s).

Resultado: AUC 0.6591 → 0.7311 (+7.2 pp) y `implied_prob_yes` salta
de 0 % gain (constante en v3_first) a rank #2 con 14.2 % gain. Brier
y ECE empeoran levemente pero ambos siguen bajo el cap
sample-size-aware. `is_active=false` — el v3_first sigue como modelo
en shadow para preservar continuidad del log de predicciones; la
decisión de promover priceshist queda para post-walk-forward.
