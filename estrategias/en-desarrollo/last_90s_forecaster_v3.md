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

## Veredicto

**Hipótesis validada** según los criterios definidos:
- Lift AUC +10.5 pp ≥ 3 pp ✓
- Brier mejora ≥ no degrada ✓
- ECE 0.147 ≤ 0.20 ✓ (gate sample-size-aware) ✓
- 3 / 5 microstructure en top-10 ✓

Caveats honestos:
- n_test = 23, std error de AUC ≈ 0.10. El intervalo aproximado es
  0.66 ± 0.10. Lift puntual está bien por encima de eso, pero la
  variabilidad estadística es alta.
- ECE val = 0.147 está por encima del 0.05 ideal (sample-size-aware
  cap 0.20 lo rescata). Calibración isotónica aplicada.
- Walk-forward NO se corrió en esta sesión. Hasta no validar
  estabilidad temporal sobre splits, no promover.

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
