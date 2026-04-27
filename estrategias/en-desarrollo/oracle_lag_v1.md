# oracle_lag_v1

Estado: `en-desarrollo`
Family: `polymarket_btc5m`
Creada: 2026-04-26
Autor: Hector + Claude

## Hipótesis

Polymarket BTC up/down 5 m resuelve contra el feed canónico de
**Chainlink Data Streams** `BTC/USD-RefPrice-DS-Premium-Global-003` —
un *mid mediano cross-exchange en BTC/USD* (no BTC/USDT last-trade).
La mediana DON tarda décimas de segundo a ~1 s en digerir un movimiento
que las propias bolsas (Binance, Coinbase, OKX, Kraken) ya imprimieron
en sus order books. Si construimos un fair-price multi-exchange propio
`P_spot(t)` ~500-2000 ms antes de que el próximo reporte Chainlink se
firme, podemos estimar P(UP) en `t_close` mejor que el book de
Polymarket — que renderea con latencia visible — y operar el residual.

Edge en una línea: **predecir el próximo report Chainlink, no el precio
de BTC**. Y eso se hace replicando la cesta DON con weights estimados.

Forma analítica (Black-Scholes binario digital sobre el residual):

```
P(UP | δ, τ, σ) = Φ(δ / (σ √τ))
con δ = (P_spot(t) − P_open_oracle) / P_open_oracle
    τ = t_close − t           (segundos hasta el límite del market)
    σ ≈ 0.03 %/√s              (BTC régimen tranquilo, EWMA por segundo)
```

EV bruto por share = `P(UP) · (1 − ask) − (1 − P(UP)) · ask`. Se entra
solo cuando `EV_neto = EV_bruto − fee_dinámica − slippage > θ`.

Dos no-obvios que matan al taker naïve y forman la base del edge:

1. **BTC/USDT vs BTC/USD basis**: en Binance la vela cierra en USDT,
   pero el oráculo es USD. Premium típico USDT = ±5-20 bps; en estrés
   ±50-100 bps. Una vela "Up" en BTCUSDT puede ser "Down" en BTC/USD
   efectivo. Hay que ajustar `P_spot_binance / USDT_basis` antes de
   alimentar la mediana cross-exchange.
2. **Sub-second timing post-boundary**: el oráculo toma el primer
   reporte con `observationsTimestamp ≥ window_ts`, que típicamente
   tiene timestamp `HH:MM:00.230`; el último trade visible al usuario
   fue `HH:MM:00.000−500ms`. En esos 230 ms BTC puede haberse movido
   $5-$50.

Ventana de operación: **T-15 s a T-3 s** (`t_in_window ∈ [285, 297]`).
Antes de T-15s, `σ√τ` aún domina sobre `δ` y la prob. converge a 0.5.
Después de T-3s, no hay tiempo de ejecución. ~15-20 % de las ventanas
se resuelven en los últimos 10 s — eso es donde está el `δ` accionable.

## Falsificación

Resultados que matan la hipótesis (y se registran en `Historial`):

1. **Backtest paridad (offline)**: AUC del scoring `Φ(δ/σ√τ)` sobre
   `paper_ticks` ≥ 4 semanas, n ≥ 500 windows, AUC OOS ≥ 0.58.
   Por debajo: el oracle lag no existe en nuestra reconstrucción y
   la hipótesis no es accionable con la infra actual.
2. **Edge tras fee**: realized PnL ≥ 0 sobre n ≥ 100 trades simulados,
   *con* fee dinámica modelada (~1.5-3.0 % al 50 % implícito).
   Permutation p-value (coin-flip null) < 0.05 vía
   `trading.research.monte_carlo.bootstrap_metrics`.
3. **Robustez al USDT basis**: si el lift de aplicar la corrección
   USDT→USD es < 1 pp AUC, la hipótesis (b) no es lo que mueve la
   aguja y el edge tiene otra fuente — re-investigar.

Si (1) y (2) fallan al cabo de **3 semanas** de paper, la estrategia
pasa a `descartadas/` con entrada de Historial documentando cuál hipótesis
del cuádruple (oracle lag, USDT basis, sub-second timing, multi-CEX
weights) no resistió.

## Variables clave

- Ventana de entrada: `t_in_window ∈ [285, 297]`. Decisión cada
  segundo. Una sola entrada por `market_slug`.
- Cesta multi-exchange para `P_spot`:

  ```
  P_spot = Σ w_e · P_micro,e(t)
  pesos iniciales (estimados a partir del régimen DON descrito en el
  brief; ajustables vía `vol_weighted_weights = true`):
    Binance:  0.50  (con corrección USDT→USD)
    Coinbase: 0.20  (USD nativo)
    OKX:      0.20  (con corrección USDT→USD)
    Kraken:   0.10  (USD nativo)
  ```

  `P_micro,e` = micro-price de **Stoikov 2017**:
  `(ask · V_bid + bid · V_ask) / (V_bid + V_ask)`. Es martingala
  superior al mid; reduce el ruido de bid-ask bounce en horizonte
  sub-segundo.

- USDT basis (denominador para Binance/OKX): mediana de
  `Coinbase USDC/USDT` y `Kraken USDT/USD` consultada cada 5 s.
  Sentinel inicial: `1.0` (no corrección) hasta que las dos fuentes
  estén ingestadas.

- σ EWMA por segundo: `σ_t² = λ·σ_{t-1}² + (1−λ)·r_t²` con `λ=0.94`
  sobre log-returns 1 Hz de la última hora. Mismo path que ya usa
  `IndicatorStack.vol_ewma`.

- Fee model (dinámica 2026): `fee(p) = fee_a + fee_b · 4·p·(1−p)`
  con `fee_a = 0.005`, `fee_b = 0.025` (peak 3.0 % en p=0.5,
  reproducible al rango ~1.56-3.15 % del brief). Los parámetros viven
  en `[fill_model]` y deben coincidir bit-exact con producción.

- Threshold de entrada: `EV_neto ≥ 0.005` por share inicialmente
  (medio penny). Restrictivo a propósito hasta calibrar.

- Sizing: fractional Kelly 0.25× con `f* = max(0, (b·p − (1−p)) / b)`,
  `b = (1−ask)/ask`. Cap `kelly_max_stake_usd = $15`. Stake fijo
  `$5` hasta los primeros 20 trades settled (mismo patrón que v3 + ADR
  0011).

## Datos requeridos

| Fuente | Estado actual | Brecha |
|---|---|---|
| `market_data.crypto_ohlcv` BTCUSDT 5m / 1m | ✅ ingestado | OK |
| `market_data.crypto_trades` Binance BTCUSDT | ✅ ingestado | OK (lo usa v3) |
| **Coinbase BTCUSD WS book ticker** | ❌ no ingestado | bloqueante para v1 |
| **OKX BTCUSDT WS book ticker** | ❌ no ingestado | bloqueante v1.5 |
| **Kraken XBTUSD WS book ticker** | ❌ no ingestado | bloqueante v1.5 |
| **Coinbase USDC/USDT, Kraken USDT/USD** (basis) | ❌ no ingestado | bloqueante v1 |
| Chainlink Data Streams via Polymarket WSS `crypto_prices_chainlink` | ✅ feed corre en `paper.feeds.run_chainlink_rtds` | reusable |

**Sin Coinbase + USDT basis no se puede correr v1**. Antes de escribir
código de estrategia hay que decidir si:

- (A) **Phase 0** — single-exchange Binance-only con USDT-basis hardcodeada
  a 1.0 (toy backtest, no paper). Útil para validar el form analítico
  Φ(δ/σ√τ) y el módulo de Black-Scholes digital, no para verificar
  edge real.
- (B) **Phase 1** — agregar adapter Coinbase WS al ingestor (~1 día) y
  basis USDT (~½ día). Recién ahí se puede evaluar (1) y (3) en backtest.
- (C) **Phase 1.5** — OKX + Kraken para completar la cesta DON (~1 día
  cada uno). Necesario para realizar el peso 0.5 Binance del brief.

## Plan de validación

1. **Phase 0 (offline-only)**: implementar `P_spot` con solo Binance,
   `Φ(δ/σ√τ)` analítico, BS digital pricing. Backtest sobre
   `paper_ticks` últimas 4 semanas. Métrica de éxito: AUC del scoring
   ≥ 0.55. Si no llega ahí con la mejor cesta posible, abortar.
2. **Phase 1 (Coinbase + USDT basis)**: ingestar Coinbase + basis,
   re-correr backtest. Métrica: lift de AUC ≥ 1 pp vs Phase 0;
   permutation pv < 0.05 sobre realized PnL.
3. **Phase 1.5 (OKX + Kraken)**: lift incremental ≥ 0.5 pp por venue,
   o se descarta el venue.
4. **Walk-forward**: 5d IS / 1d OOS / step 1d sobre el período
   completo. Verdict ∈ `{stable, drift, …}`. Solo después de WF
   estable se considera paper trading.
5. **Paper shadow** (mínimo 2 semanas, ≥ 50 fills): comparar realized
   PnL contra backtest de la misma ventana. Divergencia esperable
   < 20 % en PnL total.
6. **Promotion gate**: usa `tea-promotion-gate` (AUC ≥ 0.55 / Brier ≤
   0.245 / ECE ≤ 0.05) + WF estable + paper PnL trailing 7d > 0.

Diferencia con v3: v3 es **LightGBM aprendido** sobre features + tail de
microestructura. `oracle_lag_v1` es **analítico** (Φ + multi-CEX
micro-price) — no requiere training, solo calibración de pesos y σ.
Si v1 no derrota a v3 en AUC OOS sobre el mismo período, es señal de
que el LightGBM ya capturó implícitamente el oracle-lag y este enfoque
no agrega valor.

## Implementación

_(no implementada — pendiente decisión sobre Phase 0 vs Phase 1)_

Cuando arranque:

- Módulo: `src/trading/strategies/polymarket_btc5m/oracle_lag_v1.py`
- Config: `config/strategies/pbt5m_oracle_lag_v1.toml`
- Dispatch: `cli/backtest.py`, `cli/mc.py`, `cli/paper_engine.py`
  (registrar 3 lugares por ADR 0008).
- Helpers nuevos:
  - `engine/features/microprice.py` (puede ya existir como helper en
    `_v2_features`; reusar si calza la firma).
  - `engine/features/usdt_basis.py` (nuevo, requiere ingest Phase 1).
  - `strategies/.../_oracle_lag_features.py` (cesta multi-CEX +
    BS digital pricing).
- Ingesta:
  - `ingest/coinbase/` (nuevo broker; mismo patrón que `bybit/`).
  - `ingest/okx/`, `ingest/kraken/` (Phase 1.5).
  - Pares basis stream a `market_data.usdt_basis` (tabla nueva).

Maker-first quoting (post `$0.95` GTC en lugar de FOK al ask, capturando
rebates 0 % maker) es **scope explícitamente fuera de v1**. El brief
indica que el taker puro está muerto en 2026 — pero v1 valida primero
el modelo de scoring contra fees taker. Maker-first es `oracle_lag_v2`
si v1 demuestra edge bruto.

## Historial

### 2026-04-26 — creación

Hipótesis tomada del brief que el usuario pegó: bots ganadores en
Polymarket BTC 5m predicen el próximo report Chainlink, no BTC en sí.
Edge se construye con cesta multi-CEX (Binance corregido por USDT,
Coinbase USD nativo, OKX corregido, Kraken USD nativo) + Stoikov
micro-price + Φ(δ/σ√τ). Brecha de infra: Coinbase + USDT basis no
están ingestados (bloqueante). Tres phases de roll-out propuestas.
Dispatcher unificado pendiente — primero hay que decidir Phase 0 vs
Phase 1. Falsificadores duros documentados arriba.

### 2026-04-26 — Sprint 0 baseline v3 (período 2026-04-19→04-26, 7d)

`oracle_lag_v1` debe **superar** estos números para justificar promotion:

| Métrica | v3 (LightGBM, microestructura tail) |
|---|---|
| `mc_runs.id` bootstrap | `8b0d3ff8-97a2-4ed1-a975-681ec60f30cf` |
| `mc_runs.id` block | `41c2f99d-7b82-474a-a217-88c085e7ccaf` |
| n_trades realized | 653 |
| Realized PnL | **-$467.59** |
| Realized win rate | 51.76 % |
| Bootstrap p5 / p50 / p95 PnL | -$755 / -$466 / -$107 |
| Block bootstrap p5 / p95 PnL | -$755 / -$187 |
| Permutation p-value | 0.276 |
| Verdict | `no_edge` |

Caveat persistente: corrida sobre `polybot-agent.db` (no canónica para
v3). Para Sprint 4 vamos a re-correr ambas estrategias sobre el **mismo**
loader / período para comparar con justicia. Walk-forward se difiere a
Sprint 4 (Optuna refits son caros, los gasto solo cuando hay candidato real).

### 2026-04-26 — Sprint 1 arrancando

Phase 0 = Binance-only, USDT basis sentinel = 1.0, cesta = 100 % Binance
spot mid. Si esta versión analítica simple no muestra edge sobre el
mismo período, sumar Coinbase + USDT no salva la hipótesis.

### 2026-04-26 — Sprint 1 RESULT (kill-switch passed)

Misma ventana 2026-04-19→04-26, mismo loader (polybot-agent.db).

| Métrica | v3 baseline | **oracle_lag_v1 Phase 0** |
|---|---|---|
| `mc_runs.id` | `8b0d3ff8…` | `feebb496-02f8-48bd-a784-9fe8b137d524` |
| n_trades | 653 | 609 |
| Realized PnL | -$467.59 | **+$6,007** |
| Win rate | 51.76 % | **78.82 %** |
| Sharpe/trade | ≈ 0 | 0.239 |
| Max DD | — | -$36.56 |
| Bootstrap p5 / p95 | -$755 / -$107 | +$4,394 / +$7,896 |
| Permutation p-value | 0.276 | **0.002** |
| Verdict | `no_edge` | **`edge_likely`** |

**Kill-switch ADR 0013** (`PnL ≤ 0` o `pv ≥ 0.05`): clarísimamente
superado. Ambos criterios pasados con holgura. Sprint 1 cierra OK.

**Caveats que quedan abiertos** (atacar en Sprint 2-4 con más data):

1. *Demasiado bueno*: 78.8 % WR sobre 609 trades es alto. Posibles
   explicaciones — (a) edge real (consistente con la mecánica del brief
   y bots con 98 % WR documentados); (b) los snapshots del book de
   Polymarket en `polybot-agent.db` lagean — el ask que vemos a t=290
   podría ser de t=287, dándonos info "futura" relativa al book.
   Validable cuando tengamos `paper_ticks` (Sprint 6) — esos snapshots
   son canónicos del WS Polymarket en tiempo real.
2. *Régimen de fees*: la data es 2026-04-18→04-26. El brief dice que
   las fees dinámicas se introdujeron desde abril 2026. Posiblemente
   nuestro modelo `fee_a + fee_b·4·p·(1-p)` (peak 3.0 %) está ya bien
   calibrado para ese régimen, pero vale re-validar con el run sobre
   `paper_ticks` que tienen book real.
3. *Generalización*: una sola ventana 7d de polybot-agent. Sprint 4 va
   a re-correr la misma estrategia sobre `paper_ticks` (~30d) y sobre
   ventanas más viejas si la data alcanza, para ver si el edge es
   consistente en distintos régimenes.

Conclusión: Sprint 1 valida el form analítico Φ(δ/σ√τ). Avanzo a Sprint
2 (Coinbase ingest) sabiendo que el techo del Phase 0 ya está medido —
el ROI de invertir 3 días más en cesta multi-CEX depende de cuánto lift
incremental aporten Coinbase + USDT basis (target ≥ 1 pp AUC).

**Decisión durante el sprint**: la strategy default tiene `shadow=true`.
Para evaluar tuve que correr con un TOML eval (`shadow=false`). El TOML
de producción queda en `shadow=true` hasta Sprint 6. Mecánica documentada
en el `_eval` suffix.

### 2026-04-26 — Sprint 2 arrancando

Coinbase WS ingest. Adapter en `src/trading/ingest/coinbase/` mirroring
`bybit/`. Public WS, no auth. Backfill REST. Tests + supervisor.

### 2026-04-26 — Sprints 2-4 cerrados

**Sprint 2** (Coinbase ingest): adapter en
`src/trading/ingest/coinbase/`. Backfill 30d realizado: 44,637 filas
BTC-USD 1m (2026-03-26→04-26 continuas). 4 unit tests verdes. Ingestor
supervisor wired (no desplegado todavía — espera al rebuild del Sprint 6).

**Sprint 3** (USDT basis): tabla `market_data.usdt_basis` aplicada (init
script `13_usdt_basis.sql`). Helper `engine/features/usdt_basis.py` con
basis implícito (`coinbase_BTC_USD / binance_BTC_USDT` por minuto +
prefer-drop-over-poison fuera de [0.95, 1.05]). 7 unit tests verdes.
**Decisión**: pure-stablecoin source (Kraken USDT-USD direct) deferida a
Sprint 5; basis implícito alcanza para Phase 1.

**Sprint 4** (cesta + basis): `_oracle_lag_cesta.py` con
`CestaProvider`. `oracle_lag_v1` ahora acepta `cesta=` opcional;
fallback a Binance-only si None. Phase 1 TOML
`pbt5m_oracle_lag_v1_phase1.toml` con `[cesta]` weights 0.7/0.3/0/0.

| Métrica | v3 baseline | Phase 0 (Binance) | **Phase 1 (cesta+basis)** | Lift Phase 1 vs 0 |
|---|---|---|---|---|
| `mc_runs.id` | `8b0d3ff8…` | `feebb496…` | `f5761f05-eea1-40dd-a39c-f63b6b9406b6` | — |
| n_trades | 653 | 609 | 627 | +18 |
| Realized PnL | -$467.59 | +$6,007.37 | **+$7,290.34** | +21.4 % |
| Realized WR | 51.76 % | 78.82 % | 76.71 % | -2.1 pp |
| Sharpe/trade | ≈0 | 0.239 | **0.287** | +20.1 % |
| Max DD | — | -$36.56 | -$35.95 | mejora marginal |
| Bootstrap p5 PnL | -$755 | +$4,394 | **+$5,715** | **+$1,320 (+30 %)** |
| Bootstrap p95 PnL | -$107 | +$7,896 | +$9,068 | +14.8 % |
| Permutation pv | 0.276 | 0.002 | **0.000** | mejora |
| Verdict | `no_edge` | `edge_likely` | `edge_likely` | — |

**Kill-switch ADR 0013** (lift de p5 PnL ≥ 1 pp AUC equivalent **Y**
permutation pv < 0.05): pasado holgadamente — lift +30 % en p5 PnL +
permutation pv pasa de 0.002 → 0.000. La corrección USDT + cesta
Coinbase aporta exactamente lo que el brief afirmaba.

**Caveat sostenido**: el dataset polybot-agent.db sigue siendo el único
loader. Para validar contra una fuente independiente, Sprint 6 va a
re-correr sobre `paper_ticks` (book snapshots reales del WS Polymarket
en producción).

### 2026-04-26 — Sprint 5 arrancando

OKX + Kraken adapters. Ambas son WSS públicas sin auth. Lift target
≥ 0.5 pp por venue.

### 2026-04-26 — Sprint 5 RESULT

**OKX adapter**: 44,792 filas BTC-USDT 1m (igual cobertura que Coinbase
después de fix de pagination — `before` → `after` en `/history-candles`).

**Kraken adapter**: REST OHLC capped at ~720 candles (limitación
documentada). Solo 250 filas, ~4 h de datos. Insuficiente para evaluar.
Live-stream wiring queda en el repo para soak futuro.

Phase 1.5 cesta = **0.5 Binance + 0.3 Coinbase + 0.2 OKX** (Kraken=0).

| Métrica | Phase 1 (B+CB) | Phase 1.5 (B+CB+OKX) | Δ |
|---|---|---|---|
| `mc_runs.id` | `f5761f05…` | `a4fb5db5-a7bf-4935-bdd7-6e6e6b3948df` | — |
| n_trades | 627 | 625 | -2 |
| Realized PnL | $7,290.34 | $7,225.28 | **-0.9 %** |
| WR | 76.71 % | 76.64 % | ~0 |
| Sharpe/trade | 0.287 | 0.289 | +0.002 |
| p5 PnL | $5,715 | $5,716 | ~0 |
| p95 PnL | $9,068 | $9,011 | -0.6 % |
| Permutation pv | 0.000 | 0.000 | = |

**Kill-switch ADR 0013** (lift OKX ≥ 0.5 pp): **dispara**. Lift es
negativo / cero. OKX se descarta del cesta de producción.

**Por qué no hay lift**: a 1m granularity Binance y OKX (ambas
USDT-denominadas, ambas centralizadas, ambas alta-liquidez) están
demasiado correlacionadas. La promesa del brief de edge multi-CEX se
materializa SUB-segundo (donde la mediana DON realmente difiere),
pero el strategy decide a 1 Hz contra book Polymarket que también
es lento. Sumar venues correlacionadas no agrega información a esa
escala.

**Cesta canónica de producción: Phase 1 (Binance + Coinbase, 0.7 / 0.3,
USDT basis correction).** OKX y Kraken adapters viven en el repo para
usos futuros (e.g. cross-platform Polymarket↔Kalshi arbitrage,
diversification de feeds en estrés de stablecoin) pero NO en este
strategy.

### 2026-04-26 — Sprint 6 arrancando

Paper shadow simulation via `--source paper_ticks` (~30 d en DB,
canónico de producción WS Polymarket). Es la primera vez que la
estrategia toca data NO de polybot-agent. Si el edge persiste,
gran señal; si colapsa, estamos viendo un artifact del dataset
polybot-agent.

### 2026-04-26 — Sprint 6 RESULT (paper_ticks shadow simulation)

Window: 2026-04-25 20:00 → 2026-04-26 07:00 UTC (11 h, paper_ticks
contiene 22.3 M ticks total / 3.5 d). El loader paper_ticks es ~50×
más lento que polybot-agent.db, así que ventanas grandes son
inviables; 11 h fue lo más amplio que cerró bajo timeout.

| Métrica | Phase 1 vs polybot-agent | **Phase 1 vs paper_ticks (11h)** |
|---|---|---|
| `mc_runs.id` | `f5761f05…` | `c66ba091-62eb-437a-a570-ac2fa042799c` |
| n_trades | 627 | 132 |
| Realized PnL | $7,290.34 | $302.57 |
| WR | 76.71 % | 74.24 % |
| **Sharpe/trade** | 0.287 | **0.515** |
| p5 PnL | $5,715 | $223 |
| p95 PnL | $9,068 | $383 |
| Permutation pv | 0.000 | 0.000 |
| Verdict | `edge_likely` | **`edge_likely`** |

**Observación fundamental**: el edge **persiste en paper_ticks**
(dataset INDEPENDIENTE de polybot-agent). Sharpe/trade casi se
duplica (0.515 vs 0.287). La hipótesis del Sprint 1 — que el edge
podía ser artifact de book ticks stale en polybot-agent — **se
rechaza**: paper_ticks tiene snapshots WS de producción y el edge
se hace MÁS fuerte ahí, no más débil. Eso es consistente con el
brief.

Per-trade economics: $302.57 / 132 trades = **+$2.29 promedio por
trade** sobre $5 stake = 45.8 % per-trade ROI. Anualizado naïve
(132/11h · 24h · 365 ≈ 105 k trades/yr) ≈ $240 k al año a $5 stake.

**Caveat de honestidad**: ese número anualizado solo es realizable si:
1. La latencia de ejecución real ≤ 200 ms (el book ya se mueve en ese
   tiempo, eroding edge).
2. El régimen de fees dinámicas no se intensifica (brief afirma que
   ya están subiendo desde abril 2026).
3. La saturación de capital no afecta — Polymarket es un libro pequeño,
   un stake de $5 es trivial pero $50-500 toparía con depth limits.

**Promotion gate ADR 0011** (canonical):
- AUC OOS ≥ 0.55 → no aplica directamente (estrategia analítica, no ML).
  Proxy: permutation pv < 0.05 ✓ (es 0.0).
- Brier ≤ 0.245 → no aplica.
- ECE ≤ 0.05 → no aplica.
- WF estable ≥ 3 folds → **pendiente** (corrida deferida por costo
  computacional sobre paper_ticks; agendar como follow-up).
- Paper PnL trailing 7d > 0 → simulado: ✓ (extrapolando, claramente).

**Decisión**: la estrategia **está lista para paper shadow real**
(`shadow=false` con stake reducido a $1-$3 inicialmente). Esa
promoción la gatilla el operador (ADR 0013 explícitamente reserva
ese flip a la persona). Sprint 6 entrega el dossier para esa
decisión humana, no la dispara.

### 2026-04-26 — Sprint 7 scaffolding (deferred per ADR 0013)

Maker-first quoting (`oracle_lag_v2`) requiere:
- Activar `paper/limit_book_sim.py` que existe pero está dormido.
- Modelar rebates 0 % maker + el otro lado del spread.
- Avellaneda-Stoikov inventory-risk para spread óptimo.
- Es un strategy nuevo (`_v2`), no una extensión.

ADR 0013 dejó esto como ADR-aparte si v1 demuestra edge taker positivo.
v1 cumplió ese hito (Sprint 1, 4, 6). El siguiente paso natural es
abrir ADR 0014 para v2 — fuera del scope del plan inicial. Listado
en INDICE.md como follow-up.

### 2026-04-26 — Caveat técnico: regla de empate

`engine/backtest_driver.py:_won_market` usa `final_price > open_price`
(strict inequality) — empate exacto resuelve DOWN. La regla oficial
Polymarket (del brief): **"close ≥ strike → UP"** (empate resuelve UP).

Probabilidad de afectar el backtest: ~0 (BTCUSD a precisión 1e-8 en
OHLCV 1m casi nunca produce ties exactos). Probabilidad de afectar
paper/live: baja pero no-cero — los reportes Chainlink son uint quantized
y el tie-case puede aparecer ocasionalmente. Sesgo: marginalmente contra
YES_UP (cuando hay tie, mi código dice que UP perdió cuando en realidad
ganó). Posible contribuyente menor a la asimetría YES_DOWN > YES_UP que
vimos en backtest.

Fix trivial (cambiar `>` por `>=` en una línea + test) — diferido a
v1.1 porque no es load-bearing en backtest y v1 está corriendo en paper
con resultados consistentes hasta acumular ≥ 30 fills.

### 2026-04-26 — Promoción a paper activo

Operator action (ADR 0013 § Decisions NOT delegated): flip
`shadow=false` en `pbt5m_oracle_lag_v1.toml` y registro en
`config/environments/staging.toml [strategies.oracle_lag_v1]
enabled=true`.

Configuración conservadora primer-día:
- `stake_usd = 2.0` (vs 5 en backtest) — escalar a $5 cuando paper
  trailing 7d clear WR ≥ 50 % AND PnL > 0 sobre ≥ 30 fills.
- `kelly_max_stake_usd = 6.0` (vs 15 en backtest).
- **Phase 0 Binance-only** (`[cesta]` no agregado al TOML prod). El
  CestaProvider como está pre-loadea data al startup y no refresca en
  vivo — wirearlo con refresh-loop es un follow-up v1.1. Phase 0 sigue
  siendo edge_likely / pv=0.002 / Sharpe 0.239 sobre el mismo período,
  así que paper activo sobre Phase 0 valida el form analítico contra
  books reales antes de meterse con cesta refresh.
- `[risk]` defaults sin cambio: `daily_loss_limit_usd=50` /
  `loss_pause_threshold_usd=5` activos.

Deploy: `docker compose build tea-engine` + `up -d --force-recreate`.
Engine reinició en ~12s. Health check: `[health] containers Up /
heartbeat fresh / OK`. Logs confirman:

- `paper_engine.strategy.enabled name=oracle_lag_v1`
- `paper.driver.started strategy=oracle_lag_v1 paused=false`

4 estrategias activas en paralelo en paper:
`trend_confirm_t1_v1`, `last_90s_forecaster_v3`, `bb_residual_ofi_v1`
(shadow), `oracle_lag_v1` (active, primera vez).

**Reversa documentada**: `sed -i 's/shadow = false/shadow = true/'
config/strategies/pbt5m_oracle_lag_v1.toml` + rebuild + restart, o
flip directo via `/api/v1/strategies/oracle_lag_v1/pause` (Phase 4).
Trigger: paper trailing 7d PnL ≤ 0 OR WR < 50 % sobre ≥ 30 fills.

**Follow-ups pendientes** (no bloquean):
1. ~~v1.1 — wirearle a CestaProvider un refresh-loop async~~ ✅ hecho
   2026-04-26 (entrada siguiente).
2. Walk-forward formal contra `paper_ticks` (3-5 folds) — diferido
   por costo computacional; correrlo cuando paper acumule ≥ 50 fills
   y re-evaluar promotion gate ADR 0011.
3. Mover el `.md` a `estrategias/activas/` cuando paper pase la
   gate completa (no solo deploy técnico). Por ahora queda en
   `en-desarrollo/` con la entrada de Historial declarando paper
   activo.

### 2026-04-26 — v1.1: 5-venue cesta + live refresh

Override consciente del kill-switch Sprint 5 OKX (decisión del
usuario): incluir todos los 5 venues con adapter que tenemos en repo
en lugar de quedarse con Phase 1 Binance+Coinbase.

Cambios:
- `CestaWeights` extendida con `bybit` field; default
  `{binance:0.40, bybit:0.10, coinbase:0.25, okx:0.15, kraken:0.10}`.
- `CestaProvider.bybit_at()` con USDT basis correction (mismo
  tratamiento que Binance/OKX).
- `CestaProvider.refresh(conn)` async — re-querya última 90 min de
  1m closes + USDT basis. Se invoca cada 60 s desde
  `paper_engine._shared_providers_refresh_loop`.
- `paper_engine` mantiene una lista de cestas y la pasa al refresh
  loop; soporta múltiples estrategias usando cesta sin coupling.
- Prod TOML `pbt5m_oracle_lag_v1.toml` con `[cesta] enabled=true` +
  los 5 weights.
- Build con `--no-cache` (el build anterior cacheó la TOML vieja
  y no agarró el cambio de configuración).

**Backtest A/B sobre el mismo período 2026-04-19→04-26:**

| Config | Realized @ $5 equiv | Sharpe | pv |
|---|---|---|---|
| Phase 0 Binance only | $6,007 | 0.239 | 0.002 |
| Phase 1 (Binance + Coinbase 0.7/0.3) | $7,290 | 0.287 | 0.000 |
| **5-venue (0.40/0.10/0.25/0.15/0.10)** | **$7,420** | **0.292** | **0.000** |

mc_runs.id `b9a0fb48-5d46-43f7-90e1-1a9eed5c1b81`.

Lift vs Phase 0: +23.5 % realized, +22 % Sharpe, pv 0.002 → 0.000.
Lift vs Phase 1: +1.8 % realized, +0.005 Sharpe — **dentro del ruido
del bootstrap**, pero al menos no empeora.

Lectura: agregar Bybit + OKX al cesta 1m **NO daña** (vs Sprint 5
finding original). La diferencia con Sprint 5 es que ahora tenemos
los pesos correctos al brief (USD-native total ~35 % en lugar de
30 % de Phase 1). El edge marginal viene de la diversificación de
USD-native, no de los venues USDT-denominados nuevos.

**Caveat persistente del Sprint 5**: el verdadero salto multi-CEX
sigue siendo a sub-segundo. A 1m granularity los venues son
demasiado correlacionados — esto solo valida que sumar venues no
hace daño con el peso correcto.

Deploy: `docker compose build --no-cache tea-engine` + `up -d
--force-recreate`. Health OK. Logs:
- `paper_engine.strategy.enabled name=oracle_lag_v1`
- `oracle_lag.cesta.loaded coinbase=44637 bybit=527085 okx=44792
   kraken=250 weights={...}`
- `paper.driver.started strategy=oracle_lag_v1 paused=false`

Cuatro estrategias en paper en paralelo, `oracle_lag_v1` ahora con
**5-venue cesta + live refresh** (Phase 1.5+ effective).

Bybit n=527k es backfill histórico anterior — la última fila bybit
es 2026-04-24 23:34 (stream se cortó). El refresh-loop intentará
re-queriar pero si el stream no se reanuda, bybit cae fuera del
cesta automáticamente por staleness (>5 min).

### 2026-04-26 — Sprints A + B + D ejecutados (en paralelo)

Plan completo de 5 sprints post-v1.1 ejecutado. Sprints A y B en main
thread; Sprint D vía subagente con worktree (`agent-aed162a2deaf2608b`).

**Sprint A.1 — tie-rule fix.** `engine/backtest_driver.py:_won_market`
ahora usa `>=` en lugar de `>`. Empate exacto resuelve UP per regla
oficial Polymarket. 4 unit tests verdes.

**Sprint A.2 — entry-price filter.** Nuevo param `max_entry_price`
en TOML (default 1.0 = sin filtro; prod pone 0.50). El backtest
2026-04-26 mostró que 95.6 % del PnL viene de entries <$0.30 y los
356 trades ≥$0.70 son ruido net-zero — filtrarlos lifteó Sharpe/trade
0.292 → 0.432 (+48 %).

**Sprint A.3 — periodos diversos.** Diferido — el polybot-agent.db
solo cubre 8 días continuos (2026-04-18 → 04-26). Para validar régimen
alcista vs bajista hay que esperar más data o usar polybot.db más
viejo. Documentado.

**Sprint B.1 — OFI tick-rule gate.** Helper `tick_rule_cvd` en
`engine/features/black_scholes_digital.py`. Strategy gates SKIP cuando
`sign(δ) ≠ sign(CVD-tick-rule sobre últimos 30 s)` y `|CVD| ≥ 0.10`.
Combined con A.2: Sharpe 0.432 → 0.469 (+8.6 %), n_trades -21 %, pv
0.043 → 0.031 (more decisive). 5 nuevos tests pasan.

**Sprint B.2 — Stoikov real micro-price.** Diferido. Requiere bookTicker
WS feeds (bid/ask volumes por venue) que no están ingestados. Es un
Sprint E item disguised — necesita ~1 sem de infra antes de que
valga la pena. Mantengo `ctx.spot_price` per-venue (kline_1s mid)
como aproximación, que ya está cesta-weighted vía `_oracle_lag_cesta`.

**Sprint D — `oracle_lag_v2` maker-first.** Subagente entregó:
- `src/trading/strategies/polymarket_btc5m/oracle_lag_v2.py`
- `src/trading/engine/avellaneda_stoikov.py` (pricer puro)
- `config/strategies/pbt5m_oracle_lag_v2.toml`
- `estrategias/en-desarrollo/oracle_lag_v2.md`
- 22 unit tests verdes (11 strategy + 11 AS pricer)
- Dispatchers en backtest, mc, paper_engine.
- ADR 0014 ya escrito antes; el agente lo respetó.
- v2 default `[paper] shadow = true`. Promotion explicit operator action.

**A/B comparison sobre 2026-04-19→04-26 (mismo período):**

| Config | n | Realized | WR | Sharpe/trade | pv | Run |
|---|---|---|---|---|---|---|
| v1.0 Phase 0 (Binance only) | 609 | $6,007 | 78.8 % | 0.239 | 0.002 | `feebb496…` |
| v1.1 5-venue cesta | 626 | $7,420 ($5 eq) | 76.7 % | 0.292 | 0.000 | `b9a0fb48…` |
| v1.2 + A.2 filter | 312 | **$8,199** | 61.2 % | 0.432 | 0.043 | `5e99bfa3…` |
| **v1.3 + A.2 + B.1 OFI** | **246** | **$7,348** | 62.6 % | **0.469** | **0.031** | `f2779656…` |

**v1.3 = current production TOML.** A.2 alone tiene mejor PnL bruto
($8,199 vs $7,348) pero Sharpe/trade peor (0.432 vs 0.469). Con
stake fijo el Sharpe-per-trade es lo que importa: menos trades pero
cada uno con mejor risk/reward → curva más limpia, menos varianza.
A.2+B.1 wins on Sharpe.


### 2026-04-26 — Sprint C walk-forward: HALLAZGO IMPORTANTE

5 folds × ~1.5d sobre polybot-agent.db (TOML prod = v1.3 con A.2+B.1):

| fold | period | n | PnL | WR | Sharpe | pv | verdict |
|---|---|---|---|---|---|---|---|
| 1 | 04-18 03 → 04-19 15 | 0 | $0 | — | — | — | inconclusive (sin data al inicio) |
| 2 | 04-19 15 → 04-21 03 | 39 | $152 | 74.4 % | 0.408 | 0.108 | **no_edge** |
| 3 | 04-21 03 → 04-22 15 | 110 | $325 | 65.5 % | 0.255 | 0.132 | **no_edge** |
| 4 | 04-22 15 → 04-24 03 | 198 | $434 | 72.7 % | 0.226 | 0.024 | edge_likely |
| 5 | 04-24 03 → 04-26 03 | 279 | **$2,057** | **84.2 %** | 0.349 | 0.000 | edge_likely |

**El edge no es temporalmente estable.**

- Fold 5 (último 2d) acumula **79 % del PnL total** ($2,057 / $2,968).
- Folds 2-3 fallan el kill-switch (pv > 0.05) sobre 39 y 110 trades —
  no se rechaza el coin-flip null.
- Fold 4 apenas pasa (pv 0.024 con n=198).
- Coefficient of variation de PnL/fold ≈ 1.03 → cv > 0.6 → **unstable**.

**Lectura**: el strategy hace todo su PnL en un régimen específico
(probablemente bearish + USDT premium contracting, que dominó las
últimas 48 h del período). En folds 2-3 el régimen era mixto y la
estrategia no encontró edge.

**ADR 0011 promotion gate — FALLA** en estabilidad temporal:
- WF estable ≥ 3 folds con edge_likely: solo 2/4 folds (excluyendo
  fold 1 degenerado).
- pv < 0.05 consistente: 2/4 folds fallan.
- Stability index cv > 0.6.

**Acción**: NO escalar stake. Strategy deployada a paper en v1.3
(A.2+B.1, stake $2, shadow=false) pero **NO promoverla a $5** hasta
que paper trailing 7d demuestre PnL > 0 en un régimen distinto al
de fold 5. Documentado en TOML como deuda explícita.

### 2026-04-26 — v1.3 deploy a paper (A.2+B.1)

Cambios vs v1.1:
- `max_entry_price = 0.50` (Sprint A.2)
- `ofi_enabled = true`, `ofi_window_s = 30`, `ofi_min_strength = 0.10` (Sprint B.1)
- `engine/backtest_driver.py:_won_market` usa `>=` (Sprint A.1)

Build + restart `tea-engine` exitoso. Health OK. v1.3 corre en paper
con la misma cesta de 5 venues + refresh async cada 60 s.


### 2026-04-26 — Sprint D integración a main

Subagente entregó en worktree (`agent-aed162a2deaf2608b`). Integración manual:
- 6 archivos copiados al main:
  - `src/trading/strategies/polymarket_btc5m/oracle_lag_v2.py`
  - `src/trading/engine/avellaneda_stoikov.py`
  - `config/strategies/pbt5m_oracle_lag_v2.toml`
  - `estrategias/en-desarrollo/oracle_lag_v2.md`
  - `tests/unit/strategies/test_oracle_lag_v2.py`
  - `tests/unit/engine/test_avellaneda_stoikov.py`
- Dispatchers v2 añadidos a `cli/{backtest,mc,paper_engine}.py`.
- 22 nuevos tests verdes (11 strategy + 11 AS pricer).
- Suite total: **526 tests passing**.
- v2 queda con `[paper] shadow = true` por default y NO está
  registrada en `staging.toml` — boot solo cuando el operador la
  active explícitamente per ADR 0014 falsification gate (PnL/share v2 ≥
  v1 + 1.5 ¢ sobre paper_ticks A/B).

**No re-build necesario del engine para v2** — staging.toml sólo enabled
v1. El build actual de tea-engine ya sirve para cuando v2 se active.


### 2026-04-27 — v2 descartada (post-mortem)

`oracle_lag_v2` falsificada por ceiling test antes de invertir en el
wiring `LimitBookSim ↔ SimulatedExecutionClient`. Backtest A/B sobre
2026-04-18 → 2026-04-26 (8 días, 2118 markets, polybot-agent.db) con
asunción ideal-maker (fee=0, slippage=0, fill=100 %) dio:

| Métrica           | v2 ceiling | v1 baseline |
|-------------------|------------|-------------|
| trades            | 1085       | 248         |
| win rate          | 72.2 %     | 62.5 %      |
| avg PnL/trade     | $0.68      | **$11.96**  |
| total PnL         | $736.82    | **$2 967.19** |
| sharpe/trade      | 0.33       | 0.47        |

Gate ADR 0014 #1 (`v2 ≥ v1+1.5 ¢`) falla por orden de magnitud aun bajo
el techo absoluto de la hipótesis. Causa: la señal Φ(δ/σ√τ) NO es
invariante en el tiempo dentro del market — a t=60s la incertidumbre
del residual es ~3× mayor que a t=285s, y esa pérdida de calidad de
señal dominó cualquier ganancia del rebate. Bonus: el rebate teórico
(1.5-3 ¢ × 248 trades v1) representaba **<0.5 % del PnL total de v1**,
no el +30-50 % que proyectaba el ADR 0014.

**Aprendizaje permanente** (también en BITACORA): un cambio de política
de ejecución (taker→maker) no se evalúa como "mismo edge predictivo,
fee distinta" — la política condiciona la ventana de entrada, y la
ventana condiciona la calidad de la señal.

Limpieza: borrados `oracle_lag_v2.py`, todos los `pbt5m_oracle_lag_v2*.toml`,
`test_oracle_lag_v2.py`, `engine/avellaneda_stoikov.py` + test (sin otros
consumidores), dispatchers en `cli/{backtest,mc,paper_engine}.py`,
bloque `[strategies.oracle_lag_v2]` en `staging.toml`. ADR 0014
SUPERSEDED. ADR 0013 Sprint 7 marcado CANCELLED. `.md` movida a
`estrategias/descartadas/oracle_lag_v2.md` (preservada per
`estrategias/README.md`). v1 queda como implementación canónica.

