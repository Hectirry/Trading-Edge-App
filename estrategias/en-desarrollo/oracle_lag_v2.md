# oracle_lag_v2

Estado: `en-desarrollo`
Family: `polymarket_btc5m`
Creada: 2026-04-26
Autor: Hector + Claude

## Hipótesis

Misma hipótesis predictiva que `oracle_lag_v1` — el residual
`Φ(δ/σ√τ)` sobre la cesta multi-CEX corregida por USDT basis le gana
al book de Polymarket porque el oráculo Chainlink lagga décimas de
segundo. **La diferencia es la política de ejecución**: en lugar de
comerse la fee dinámica taker (1.5-3.0 % al p=0.5), v2 postea órdenes
limit GTC en el lado maker y captura el rebate 0 %.

Per-trade swing económico: la fee taker en v1 era 1.5-3.0 %; v2 paga
0 %. A los edges netos típicos de v1 ($0.04-0.08/share post-fee), bajar
la fee captura 1.5-3.0 ¢/share extra — un retorno relativo de
+30-50 % sobre la misma señal predictiva.

Edge en una línea: **misma señal que v1, fee taker reemplazada por
rebate maker via Avellaneda-Stoikov quoting**.

## Variables clave

- Ventana de entrada: `t_in_window ∈ [60, 297]`. Mucho más amplia que
  v1 ([285, 297]) — el maker puede campar el book en lugar de
  necesitar EV deterministic en los últimos 15 s. Una sola fill por
  `market_slug` (re-quotes sí, multi-fill no — `max_active_quotes_per_market = 1`).
- Cesta multi-exchange: idéntica a v1 (5 venues, mismo provider).
- Pricer A-S (`src/trading/engine/avellaneda_stoikov.py`):

  ```
  spread*  = γ · σ² · (T - t) + (2/γ) · ln(1 + γ/k)
  reservation_price = mid − q · γ · σ² · (T - t)
  ```

  Defaults: γ = 0.1, k = 5.0. q = 0 porque v2 corre flat por ventana.
- Floor del offset: `limit_offset_bps = 50` (0.005 en unidades de
  prob.). El A-S half-spread y este floor compiten — gana el más ancho.
- Cancel/re-quote: si el EV cae ≥ `cancel_threshold_drop_bps = 30 bps`
  vs. el EV at-quote-time, o si el limit price calculado se mueve
  ≥ 30 bps, cancelar y re-quotear. Mínimo 2 s entre cancels (≤ 30
  cancels/min/strategy = 1 % del rate-limit Polymarket).
- Sizing: idéntico a v1 — fixed $2 stake en primer deploy, kelly-frac
  0.25 después de 20 trades.

## Falsificación

ADR 0014 § "Falsification" define dos gates concretos sobre el mismo
período de paper_ticks que v1:

1. **PnL/share**: realized PnL/share v2 ≥ realized PnL/share v1 + 1.5 ¢.
   (Captura ≥ ½ de la fee taker que v1 paga.)
2. **Maker fill rate**: ≥ 40 % de los quotes emitidos terminan
   filleados antes del close del market. Por debajo, el quote-pricing
   está sobre-conservador y la mayoría de los quotes nunca cruzan.

Si **ambos** gates pasan en n ≥ 100 markets, v2 va a `activas/`.
Si alguno falla, v2 a `descartadas/` con entrada de Historial — la
hipótesis quedaría: el rebate maker no compensa el fill-rate hit, y
v1 (taker) sigue siendo la implementación correcta de la señal.

## Parámetros provisionales

```toml
[params]
entry_window_start_s = 60.0
entry_window_end_s = 297.0
sigma_lookback_s = 90.0
sigma_min_ticks = 60
ewma_lambda = 0.94
ev_threshold = 0.005
usdt_basis_phase0 = 1.0

[execution]
mode = "maker"
limit_offset_bps = 50.0
gamma_inventory = 0.1
k_order_arrival = 5.0
cancel_threshold_drop_bps = 30.0
cancel_min_interval_s = 2.0
max_active_quotes_per_market = 1
```

## Datos requeridos

Idénticos a v1 — la estrategia comparte el `CestaProvider` y el
`black_scholes_digital`:

- `market_data.crypto_ohlcv` BTCUSDT 5m + 1m Binance (sí, ingestado).
- `market_data.crypto_ohlcv` Coinbase, Bybit, OKX, Kraken 1m (sí,
  Sprint 2-5 de ADR 0013).
- `market_data.usdt_basis` 1m (sí, Sprint 3 ADR 0013).
- `market_data.paper_ticks` ≥ 30 días para A/B contra v1.

No requiere ingest nuevo — el paquete completo de v1 cubre v2.

## Implementación

- Módulo: `src/trading/strategies/polymarket_btc5m/oracle_lag_v2.py`
- Helper compartido: `_oracle_lag_cesta.py` (no modificado).
- Pricer: `src/trading/engine/avellaneda_stoikov.py` (nuevo).
- Config: `config/strategies/pbt5m_oracle_lag_v2.toml`
- Dispatch:
  - `src/trading/cli/backtest.py:_load_strategy`
  - `src/trading/cli/mc.py:_build_factory`
  - `src/trading/cli/paper_engine.py:_load_strategy`
- Limit-fill simulation: `src/trading/paper/limit_book_sim.py` (existente,
  no modificado en este sprint — pendiente wiring al `SimulatedExecutionClient`
  para que el backtest de v2 efectivamente respete el modelo de fill maker;
  ver Caveats abajo).
- Introducido en commit: _(pendiente)_

## Plan de validación

1. **Unit tests verdes** (este sprint):
   - `tests/unit/engine/test_avellaneda_stoikov.py` — golden vector,
     edge cases, monotonía.
   - `tests/unit/strategies/test_oracle_lag_v2.py` — 7+ tests de
     decision flow (window, EV gate, AS limit calc, shadow, cancel
     on edge drop, requote throttle).

2. **Backtest A/B vs v1** (operador post-merge):
   - Mismo período: últimos 30 días de paper_ticks.
   - Mismas KPIs: PnL/share, maker fill rate, n_trades, Sharpe/trade.
   - Decisión per ADR 0014 falsification gates.

3. **Walk-forward** (si A/B sale positivo): 5d IS / 1d OOS sobre
   los mismos 30 días.

4. **Paper shadow**: corre con `[paper] shadow = true` por default.
   Operator flip post-validation per ADR 0011.

## Caveats

- **`limit_book_sim.py` no está wired al `SimulatedExecutionClient`**
  todavía. La estrategia emite `Decision(order_type=GTC,
  limit_price=...)` pero el `SimulatedExecutionClient` actual solo
  modela FAK fills. En backtest la tubería de fill termina aplicando
  el ev-gross con slippage = 0 (que aproxima maker fill at limit
  exactly). El refactor del exec_client para invocar `LimitBookSim`
  por tick es Sprint D+1 (separado, mayor scope, requiere también
  cambios en el `backtest_driver` para que el clock-1Hz pase
  `on_tick` al limit book).
- **`fill_probability` decay model**: el blueprint pide modelar la
  probabilidad de fill como función de la distancia al mid. La impl
  actual de `LimitBookSim` usa un modelo binario (cruzaste el limit ↔
  fill). El refactor con `fill_probability(distance_bps)` es trivial
  añadir cuando el wiring al exec_client se haga.
- **Rate-limit budget**: 1 cancel / 2 s / strategy = ≤ 30/min. Bajo el
  3,000/10s account-wide. Ningún risk de saturar el budget en
  funcionamiento normal.

## Historial

### 2026-04-26 — creación

ADR 0014 abre el sprint. v1 ya en paper shadow (post-Sprint 6).
Entry window se amplía a [60, 297] vs [285, 297] de v1 — la lógica
es que el maker camp es viable mucho antes que el window-end-snipe del
taker. El A-S pricer queda como módulo puro reusable
(`src/trading/engine/avellaneda_stoikov.py`) con golden-vector tests
para regression-protect.
