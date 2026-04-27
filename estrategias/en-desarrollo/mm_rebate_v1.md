# mm_rebate_v1

Estado: `en-desarrollo`
Family: `polymarket_btc15m` (nueva — primera estrategia 15m de TEA)
Creada: 2026-04-27
Autor: Hector + Claude

## Hipótesis

Capturar spread + capturar rebate proporcional de Polymarket (20% del pool
de taker fees colectados en Crypto, payout diario) + evitar fee taker
dinámica operando como market maker neutral en zonas de precio extremas,
evitando la zona muerta 0.40-0.60 donde compiten bots informados con
ventaja de p_fair. La economía no depende de predecir dirección de
P(T)-P(0); depende de capturar spread mientras se gestiona inventario
q ≈ 0.

**Erratum sobre ADR 0014 (2026-04-27)**: el ADR decía "0% maker rebate"
y proyectaba "negative rebate if Polymarket adds one in 2026Q3". Verificado
en docs.polymarket.com el 2026-04-27: el programa **ya está activo** con
20% en Crypto / 25% en otras categorías generales / 0% en Geopolitics.
Mecánica: `rebate_share = (my_fee_equivalent / total) × pool`, no per-fill
sino proporcional al volumen-share del maker en el market. ADR de erratum
a 0014 pendiente de abrir.

## Variables clave

- **Horizonte**: 15m (window 900s). Entry desde t≈60s; cancel-replace continuo.
  No cotiza últimos 60s (τ_terminal).
- **Reservation price (Avellaneda-Stoikov adaptado a binarias)**:
  `r(t,q) = p_fair(t) − q · γ · σ²_BM(t) · (T-t)`
- **Spread óptimo total**:
  `δ_total(t) = γ · σ²_BM(t) · (T-t) + (2/γ) · ln(1 + γ/k)`
- **σ_BM(t)** = `sqrt(p_fair · (1-p_fair) · (T-t)/T)` — Brownian-bridge
  variance, → 0 cuando t→T.
- **p_fair**: Brownian-bridge p_BM puro (sin ensemble, ya falsificado).
- **γ**: aversión al riesgo de inventario, tunable.
- **k(δ)**: intensidad de fills empírica per-bucket.
- **q**: inventario neto en shares de YES (signed), enforce cap en USDC notional.
- **Zona muerta excluida**: [0.40, 0.60]. NO cotizar si p_market en esa zona.

## Falsificación (gates ADR-0011 análogos para MM)

Step 0 actual (este documento) entrega verdict GO/NO-GO. Para
**promoción a active** el gate completo es:

| Métrica | Threshold |
|---|---|
| Sharpe diario neto OOS | ≥ 1.5 |
| Brier(p_BM(t=mid_window), 1{outcome_up}) | ≤ 0.245 |
| \|E[q]\| ≤ 0.1·q_max en ventana 1h | (inventory neutrality) |
| Adverse selection ratio 5s | < 0.6 |
| Maker fill rate post-fee | ≥ 30% |
| Spread-attribution / total_pnl | > 0.5 |
| **Canary**: taker_fee_paid / total_pnl_gross (rolling 7d) | < 5% |
| Queue position calibration error | < 10% (Tier 1, gate de promotion) |

Si en el camino: cualquier bucket candidato pierde su gate ADR-0011 análogo
durante walk-forward → bucket descartado. Si todos los buckets candidatos caen → estrategia
descartada.

## Parámetros provisionales

```toml
[params]
entry_window_start_s   = 60          # cotizar a partir de t=60s
entry_window_end_s     = 840         # parar últimos 60s (τ_terminal)
gamma_inventory_risk   = 0.5         # aversión inventario, AS γ
spread_floor_bps       = 50          # δ_total mínimo (50bps = 0.5¢)
spread_ceiling_bps     = 500         # safety
zona_muerta_lo         = 0.40
zona_muerta_hi         = 0.60
q_max_shares           = 200         # cap inventory en shares (math)
q_max_usdc             = 10.0        # cap inventory en USDC (safety)
taker_fee_a            = 0.005
taker_fee_b            = 0.025
sigma_lookback_s       = 90
ewma_lambda            = 0.94

[sizing]
stake_nominal_usd      = 5.0
kelly_fraction         = 0.25        # disabled in V1 — fixed stake until 20 fills
kelly_min_trades       = 20

[fill_model]
fill_probability       = 1.0         # MM resting limit — fills via limit_book_sim
apply_fee_in_backtest  = true
fee_k                  = 0.05        # parabolic taker fee (only on forced flows)

[risk]
bypass_in_backtest     = true        # standard RiskManager bypass (5m/15m parity)
cooldown_seconds       = 0           # MM never cools off intentionally
max_position_size_usd  = 15.0
daily_loss_limit_usd   = 50.0
daily_trade_limit      = 999999
min_edge_bps           = 0           # MM doesn't gate on directional edge
min_z_score            = 0.0
min_pm_depth_usd       = 5.0
skip_if_spread_bps     = 500

[mm_safety]
# NEW gates: NOT bypassable in backtest. Protect against simulator bug
# or strategy degeneration.
inventory_cap_usdc           = 10.0
cancel_fill_ratio_max        = 20.0
cancel_fill_window_minutes   = 5
cancel_fill_kill_threshold   = 2     # 2 disparos en 60min = kill mercado
cancel_fill_kill_window_min  = 60
cancel_fill_resume_min       = 15
taker_fee_canary_pct         = 0.05  # < 5% rolling 7d
taker_fee_canary_window_days = 7
tau_terminal_s               = 60    # no cotizar últimos 60s

[paper]
capital_usd            = 1000.0
daily_loss_alert_pct   = 0.03
daily_loss_pause_pct   = 0.05
shadow                 = true        # always true at first paper deploy
```

## Datos requeridos

- ✅ `market_data.polymarket_markets` 15m — 605 markets ingested 2026-04-27 (Step -1.b).
- ✅ `market_data.polymarket_prices_history` 15m — 1.59M rows.
- ✅ `market_data.polymarket_trades` 15m — 970K rows.
- ✅ `research.market_manifest_btc15m` — coverage flags persisted, last_validated_at 2026-04-27.
- ⚠ `paper_ticks` 15m — empty hasta ahora; live-tap habilitado 2026-04-27 16:36 UTC, acumulando hacia adelante.
- ⚠ Pre-2026-04-28 no hay 15m coverage en TEA (Gamma /events límite estructural).

## Implementación

_(no se implementa hasta green-light Step 1 — formalización del modelo)_

Cuando se implemente:
- Módulo: `src/trading/strategies/polymarket_btc15m/mm_rebate_v1.py`
- Config: `config/strategies/pbt15m_mm_rebate_v1.toml`
- Dispatch: `src/trading/cli/backtest.py` + `src/trading/cli/paper_engine.py`
  (ADR 0008 — sin auto-discovery)
- Helpers nuevos:
  - `src/trading/strategies/polymarket_btc15m/_mm_features.py`
    (σ_BM(t), reservation price, optimal spread, c_b calibration)
  - `src/trading/paper/limit_book_sim.py` — wired into both backtest and paper
    paths; ADR aparte declara Tier 1 (<15% fill count error / <10% spread captured)
    como gate para correr backtest, Tier 2 (<10%/<5%) como soak target.
  - `src/trading/engine/risk.py` — agregar `MMSafetyGuard` (no bypassable)
    junto al `RiskManager` (bypassable) existente.

## Plan de validación

### Step −1.a: Probe Gamma API ✅ DONE 2026-04-27

599 markets enumerados (5.8 días útiles), `seriesSlug='btc-up-or-down-15m'`,
no `series_id` numérico → enumeración via `/events` global con filter
client-side. Manifesto persistido `/tmp/step_minus_1a_result.json`.

### Step −1.b: Backfill + adapter extension ✅ DONE 2026-04-27

- Adapter extendido: `discover_markets(slug_pattern=SLUG_PREFIX_15M)`,
  `backfill_market_trades` (Data API `/trades`), `backfill_market_prices_history`
  (CLOB con startTs/endTs).
- Init SQL `14_market_manifest.sql` aplicado.
- Backfill ejecutado: **1,591,784 prices + 969,817 trades** sobre 599 markets.
- Live-tap forward activado vía `make deploy-staging` 2026-04-27 16:36 UTC.
- Tests: 6/6 pass (regression cubre 5m flow intacto + 15m guard + trades pagination).

### Step 0: Caracterización empírica ✅ DONE 2026-04-27 (este documento)

Verdict + tabla de buckets — ver sección **Resultado de Step 0** abajo.

### Step 1: Formalización del modelo (próximo, condicionado a verdict GO)

- Implementar `σ_BM(t) = sqrt(p_fair·(1-p_fair)·(T-t)/T)` en `_mm_features.py`.
- Implementar `reservation_price(t, q, p_fair)` y `optimal_spread(t, σ, k, γ)`.
- Implementar `fee_taker(p) = fee_a + fee_b · 4·p·(1-p)` (replicado de oracle_lag_v1).
- Estimación online de `k(δ)` con ventana rolling 7d (warm-start con el fit
  empírico del Step 0).
- Tests unitarios contra valores conocidos (p=0.5 boundary, t=0/T=900 boundary).
- **Sin escribir clase de estrategia todavía** — solo helpers + tests.

### Step 2: Strategy class

- Definir `Action = PostQuote | CancelQuote | ReplaceQuote` como sum type
  frozen dataclasses (#7a aprobado).
- Extender `StrategyBase.on_tick(ctx) → list[Action]` opcional, no reemplaza
  `should_enter` (#7b aprobado).
- Crear módulo `mm_rebate_v1.py` heredando `StrategyBase` con `on_tick`.
- Crear TOML con secciones acordadas (params, sizing, backtest, fill_model,
  risk, mm_safety, paper).
- Editar dispatch en `cli/backtest.py` + `cli/paper_engine.py`.
- ADR aparte para `limit_book_sim.py` con acceptance criteria Tier 1/Tier 2.

### Step 3: Backtest

- Window: 5.8 días disponibles + acumulado live-tap.
- Métricas: Spread captured per fill, fee_avoided, net per fill, fill rate,
  inventory turnover, max inventory drawdown, adverse-selection ratio,
  cancel/fill ratio, PnL attribution, Sharpe diario.
- NO `sharpe_annualized` (meaningless per CLAUDE.md).
- Persistir `research.backtests` + `research.backtest_trades`.

### Step 4: Walk-forward

- 5d IS / 1d OOS / 1d step (defaults TEA).
- Stability index sobre métricas MM (re-definido para esta estrategia,
  no AUC).
- Persistir `research.walk_forward_runs`.
- Si stability ≥ 3/N folds → candidato a paper.

### Step 5: Monte Carlo

- Bootstrap trade-vector (cheap) primero.
- Block bootstrap si bootstrap pasa.
- Persistir `research.mc_runs`.
- Verdict ∈ {edge_likely, no_edge, inconclusive}.

### Step 6: Paper trading

- Habilitar `staging.toml [strategies.mm_rebate_v1] enabled = true`.
- Soak ≥ 7 días, monitoreo diario.
- Comparar paper-vs-backtest (cron domingo); >2σ desviación → investigación.
- Step 0 v2 corre 2026-05-27 con muestra acumulada ≥30 días → re-validation.

### Step 7: Promoción (gated)

- Skill `tea-promotion-gate` aplicado.
- Re-discutir gates ADR 0011 análogos antes de flipear `is_active=true`.

## ADRs requeridos antes de Step 2

1. **ADR — Excepción a Design.md Parte III**: justificar latencia-no-binding
   en binarias 15m (RTT Dublín-CLOB ≤2ms vs horizonte 15min). Documentar
   por qué oracle_lag_v2 muerto-2026-04-27 no falsifica este caso (diferente
   estrategia: bilateral inventory-neutral vs maker-first single-side).

2. **ADR — limit_book_sim.py**: tier 1 (<15% fill count / <10% spread
   captured) como gate para usar en producción de backtest, tier 2
   (<10%/<5%) como soak target Step 6+. Acceptance criteria explícitos.

3. **ADR — `Action` sum type + `on_tick → list[Action]`**: extender
   `StrategyBase` aditivamente para soportar quoting continuo sin romper
   las 3 estrategias activas.

## Resultado de Step 0

**Verdict: AMBIGUOUS** (data-limited, not strategy-falsified).

- Strict ALL-criteria gate → NO-GO (no bucket pasa los 5).
- Real-world reading → 3 below-zona buckets pasan **4/5 criterios**, fallando solo
  en `front_ratio` que es un proxy roto sin book data.
- Hipótesis NO falsificada — el gate `front_ratio` está data-blocked.

Tabla de buckets (resumen, computada sobre 504 markets resolved + 962K trades):

| Bucket | N trades | k(2¢)/min | adv_30s_signed | adv_5s_ratio | net/fill USDC | E[I/h] USDC | Criterios |
|---|---|---|---|---|---|---|---|
| **0.15-0.20** ★ | 39,171 | 37.4 | -0.029 (favorable) | 0.024 | +5.38 | +12,046 | 4/5 |
| 0.20-0.30 | 74,728 | 29.9 | -0.026 (favorable) | 0.024 | +3.45 | +6,181 | 4/5 |
| 0.30-0.40 | 95,500 | 28.2 | -0.021 (favorable) | 0.024 | +2.02 | +3,411 | 4/5 |
| 0.40-0.60 (zona muerta) | 292,626 | 0.13 | -0.002 | 0.037 | +0.74 | +5.6 | excluded |
| 0.60-0.70 | 103,314 | 30.3 | +0.016 (adverse) | 0.047 | -0.04 | -81 | 3/5 |
| 0.70-0.80 | 83,175 | 34.4 | +0.026 (adverse) | 0.052 | -0.33 | -692 | 3/5 |
| 0.80-0.85 | 37,014 | 33.7 | +0.025 (adverse) | 0.054 | -0.27 | -548 | 3/5 |

Hallazgo estructural: **adverse selection asimétrica entre below-zona y above-zona**.
- Below-zona (YES < 0.40): consensus es Down. Late YES BUYers son momentum chasers,
  sus compras NO mueven precio en su favor (moves favorables al maker que vendió YES).
- Above-zona (YES > 0.60): consensus es Up. Late YES BUYers son informed (correctos),
  sus compras SÍ mueven precio en su favor (moves adversos al maker).
- Esto NO se ve en una formulación AS pura — es un edge específico de prediction
  markets binarios y matchea la intuición de "informed-vs-momentum" cuando el
  mercado tiene un consensus claro.

**Bucket nominee para V1: 0.15-0.20** (highest E[I/h] candidate).

Otros findings:
- Cross-arb proxy frequency: 0.16% (la condición YES+NO≈1 se cumple casi siempre;
  MM neutrality assumption se valida).
- c_b = std_emp/std_theo: en rango 0.91-1.21 para todos los buckets candidatos
  (excepto zona muerta donde c_b=0.04, esperado por densidad de mercados ahí).
  El modelo Brownian-bridge es sound a este granularidad.
- Realized vol 30s/120s ≈ idénticos (~7%) — limitación de fidelity 1-min de
  prices_history (no podemos resolver microestructura sub-minuto).

Limitaciones metodológicas que generan el AMBIGUOUS:
1. **No bid/ask explícito**: prices_history es 1 sample por minuto, no captura book
   state. Spread inferido de BUY-vs-SELL prices intra-minute (proxy unreliable).
2. **Fidelity 1-min para adverse selection**: Δp_yes a 5s/10s frecuentemente devuelve
   el mismo sample minuto-current → señal subestimada para adverse_5s/10s.
3. **front_of_queue_ratio sin book data**: proxy "≥2 trades same side same price
   intra-minute" no capta queue position real. Todos los buckets fallan este gate
   por este motivo, no por mérito de la estrategia.

Estos límites son ESTRUCTURALES con la data disponible. **Step 0 v2 con paper_ticks
30d (acumulado desde 2026-04-27 16:36 UTC vía live-tap)** los resuelve:
- paper_ticks tiene bid/ask + depth + spread cada 1Hz
- Adverse selection sub-minuto medible directamente
- Queue position computable desde book ladder

Reporte HTML persistido en
`src/trading/research/reports/20260427T165724Z_step0_mm_rebate_v1.html`.
JSON en `/tmp/step_0_report.json` (también con tabla corregida).

## Historial

### 2026-04-27 — creación + Step −1 + Step 0 (sesión inicial)

Pivot desde `bb_residual_ofi_v1` (falsificada misma fecha, 3 corridas WF) y
`oracle_lag_v2` (falsificada misma fecha, ceiling test). Hipótesis MM neutral
sobre 15m. Steps −1.a, −1.b, 0 ejecutados en una sola sesión:

- Step −1.a: 599 markets discoverable via Gamma /events (5.8 días útiles).
- Step −1.b: 1.59M prices + 970K trades ingested + adapter extendido + live-tap
  forward deployed.
- Step 0: caracterización + verdict (ver sección **Resultado de Step 0**).

PR `feature/mm-rebate-v1-step-minus-1` abierto, no merged hasta review.
