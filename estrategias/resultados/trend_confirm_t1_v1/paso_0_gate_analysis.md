# Paso 0 — gate-firing analysis (trend_confirm_t1_v1 → t2_v0 prep)

Fecha: 2026-04-25
Branch: `feature/trend_confirm_t2_v0`
Commit instrumentación: `f808b94`
Backtest ID: `08ce6f7f-7c23-41ac-b87a-59cb6d5ae36f`
Ventana: 2026-04-18T00:00 → 2026-04-24T22:00 UTC
Source: `polybot_sqlite` → `/btc-tendencia-data/polybot-agent.db`

## Caveat de paridad con baseline d8f589a

| | Baseline d8f589a | Re-run instrumented |
|---|---|---|
| n_trades | 590 | **332** |
| win_rate | 76.1% | 80.4% |
| total_pnl | $83.52 | $948.08 |
| avg_pnl/trade | $0.14 | $2.85 |

Mismo `params_hash` (`c5a0d6788af60179`), misma ventana, mismo source format.
Diferencia de 258 trades viene de la **DB física distinta**: el run d8f589a usó
`/polybot-btc5m-data/polybot.db` (mount ahora vacío); este re-run usa
`polybot-agent.db` montado en `/btc-tendencia-data/`. **No es bit-exact**.
La pregunta de Paso 0 (correlación entre gates) sigue siendo respondible
porque es propiedad de la estrategia, no de los datos — pero el verdict
MARGINAL del baseline citado en el spec **no es comparable directamente**
con esta corrida.

## Fire-rate por gate (sobre los 332 entries)

```
f1 fracdiff           = 71.4%
f2 autocorr30         = 54.8%
f3 cusum              = 87.0%
f4 microprice         = 76.5%
f5 mc_bootstrap       = 96.7%
f6 candle60s          = 84.6%
f7 prior_trend600s    = 87.0%
```

f5 fires 96.7% — virtualmente determinístico dentro del subset de entries.
Esto comprime su poder discriminativo dentro del análisis condicional a
entry. Lo mismo en menor grado para f3 y f7.

## Matriz de correlación pairwise (Pearson sobre 0/1)

```
            f1     f2     f3     f4     f5     f6     f7
f1       +1.00  -0.28  -0.16  +0.39  -0.04  -0.20  -0.13
f2       -0.28  +1.00  +0.03  -0.30  -0.07  -0.08  -0.06
f3       -0.16  +0.03  +1.00  -0.19  +0.03  -0.14  +0.04
f4       +0.39  -0.30  -0.19  +1.00  +0.02  -0.18  -0.17
f5       -0.04  -0.07  +0.03  +0.02  +1.00  -0.08  -0.07
f6       -0.20  -0.08  -0.14  -0.18  -0.08  +1.00  -0.06
f7       -0.13  -0.06  +0.04  -0.17  -0.07  -0.06  +1.00
```

Top 5 pares por |corr|:

| par | corr | descripción |
|---|---|---|
| f1 ↔ f4 | **+0.388** | fracdiff vs microprice |
| f2 ↔ f4 | -0.303 | autocorr30 vs microprice |
| f1 ↔ f2 | -0.280 | fracdiff vs autocorr30 |
| f1 ↔ f6 | -0.196 | fracdiff vs candle60s |
| f3 ↔ f4 | -0.193 | cusum vs microprice |

## Verdict del Paso 0 vs hipótesis del spec

> **Hipótesis del spec:** "redundancia entre gates f1, f2, f6, f7 (todos miden trending a distintas escalas) — corr > 0.5 confirma; corr < 0.3 reportar y consultar."

**Hipótesis REJECTED.** Pares dentro del cluster putativo {f1, f2, f6, f7}:

| par | corr | absoluto |
|---|---|---|
| f1 ↔ f2 | -0.28 | 0.28 |
| f1 ↔ f6 | -0.20 | 0.20 |
| f1 ↔ f7 | -0.13 | 0.13 |
| f2 ↔ f6 | -0.08 | 0.08 |
| f2 ↔ f7 | -0.06 | 0.06 |
| f6 ↔ f7 | -0.06 | 0.06 |

**Todas |corr| < 0.3.** No hay redundancia detectable en ese cluster. De
hecho, la mayoría son **negativamente correlacionadas** — los gates
disparan en condiciones distintas, no las mismas.

El par más fuerte (f1 ↔ f4 = **+0.39**) cruza el cluster A propuesto
(momentum/trend) con el cluster B (microestructura), y aún así no llega
al umbral de 0.5.

Per spec: **STOP y consultar antes de Paso 1** — el reagrupamiento en
clusters ortogonales puede no aportar valor porque los gates ya son
razonablemente ortogonales entre sí.

## Counter-finding: f5 (MC bootstrap) parece carrying

Combos que entran con `f5=False` pierden sistemáticamente (N=9 trades
across 5 combos, todos -$5):

| combo | gates | n | win_rate | total_pnl |
|---|---|---|---|---|
| 1011011 | f1,f3,f4,f6,f7 | 3 | 0% | -$15 |
| 1110011 | f1,f2,f3,f6,f7 | 3 | 0% | -$15 |
| 1101011 | f1,f2,f4,f6,f7 | 2 | 0% | -$10 |
| 0111011 | f2,f3,f4,f6,f7 | 2 | 0% | -$10 |
| 1111011 | f1,f2,f3,f4,f6,f7 | 1 | 0% | -$5 |

N=9 es bajo para concluir definitivamente, pero el patrón es 0/9 en
ganadoras. Sugiere que **f5 ya está haciendo el trabajo de validación
estadística que cluster C del spec quería aislar** — el spec acertó al
ponerlo en su propio cluster, pero la implicación para Paso 1 es que
f5 debería ser un **gate hard required**, no un voto entre tantos.

Combos top por N (sin trades-perdedores estructurales):

| combo | gates | n | win_rate | avg_pnl |
|---|---|---|---|---|
| 1011111 | f1,f3,f4,f5,f6,f7 (6) | 56 | 83.9% | $3.14 |
| 0110111 | f2,f3,f5,f6,f7 (5) | 47 | 70.2% | $2.04 |
| 1111111 | todos los 7 | 41 | 85.4% | $3.28 |
| 0011111 | f3,f4,f5,f6,f7 (5) | 22 | 81.8% | $2.89 |
| **1001111** | **f1,f4,f5,f6,f7 (5)** | **21** | **100%** | **$4.75** |

## Recomendación

**No avanzar a Paso 1 tal cual está descrito en el spec.** Dos opciones
de pivot:

1. **Mantener Paso 1 pero revisar la regla de cluster.** Dado que la
   redundancia esperada no aparece, el reagrupamiento en clusters no
   compra ortogonalidad nueva. Una alternativa simple: **convertir f5
   en hard-required gate** (no shadow, no vote, requerido) y dejar el
   resto del stack como suma con threshold más bajo. Apuesta: el lift
   del Paso 1 ya está implícitamente en f5; el cluster regrouping no
   añade.

2. **Saltarse Paso 1** y pasar directo a Paso 2 (sizing fee-aware), que
   no depende de la hipótesis de redundancia. Paso 3 (τ-buckets) tampoco
   depende.

Esperando go-ahead del usuario.

## Artefactos

- `src/trading/research/reports/gate_correlation_matrix.csv` (CSV)
- `src/trading/research/reports/gate_correlation_heatmap.png` (PNG)
- `src/trading/research/reports/gate_combinations_pnl.csv` (CSV, 27 combos)
- Script reproducible: [scripts/analyze_gate_firings.py](../../../scripts/analyze_gate_firings.py)
- HTML del backtest: `src/trading/research/reports/20260425T005624Z_polymarket_btc5m_trend_confirm_t1_v1_c5a0d6788af60179.html`

Para reproducir desde el branch:

```bash
PG_PW=$(docker compose exec -T tea-postgres bash -c 'echo $POSTGRES_PASSWORD' | tr -d '\r')
TEA_PG_DSN="postgresql://tea:${PG_PW}@127.0.0.1:5434/trading_edge" \
    .venv/bin/python scripts/analyze_gate_firings.py \
    --backtest-id 08ce6f7f-7c23-41ac-b87a-59cb6d5ae36f \
    --out-dir src/trading/research/reports
```
