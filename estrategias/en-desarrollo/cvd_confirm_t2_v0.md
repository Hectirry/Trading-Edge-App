# cvd_confirm_t2_v0

Estado: `en-desarrollo`
Family: `polymarket_btc5m`
Creada: 2026-04-24
Autor: Hector + Claude

## Hipótesis

En la ventana T-3min → T-2min de un mercado Polymarket BTC 5m, la señal
de dirección de `trend_confirm_t1_v1` es correcta en conjunto pero tiene
falsos positivos cuando el order-flow spot contradice al precio. Agregar
un gate basado en CVD (cumulative volume delta) de Binance 1m sobre los
últimos 3 minutos debería filtrar ~20% de trades perdedores manteniendo
la mayoría de ganadores.

## Variables clave

- Horizonte / ventana: hereda de trend_confirm_t1_v1 (entry_horizon_s=150, tol=30).
- Señal de dirección: hereda (`sign(delta_bps)` con `|delta_bps| >= 1.0`).
- Gate nuevo (f8): `cvd_3m_side == decision_side` con `|cvd_3m| >= cvd_min_usd`.
- Threshold de confirmaciones: sube `min_confirmations` de 5 → 6 (y `high_price` de 6 → 7).
- Sizing: hereda (stake_usd=10, kelly_fraction=0.25).

## Falsificación

Mata la hipótesis cualquiera de:

- En backtest 14d (2026-04-10 → 2026-04-24) con n ≥ 50 trades: `sharpe_per_trade`
  no supera a `trend_confirm_t1_v1` sobre la misma ventana, o
- `total_pnl` cae > 20% vs baseline sin compensar con reducción de mdd > 30%, o
- El gate es silencioso (< 5% de trades del baseline afectados).

## Parámetros provisionales

Heredan de `pbt5m_trend_confirm_t1_v1.toml`, agregar:

```
cvd_window_s = 180
cvd_min_usd = 50000       # provisional; calibrar con histograma de CVD
min_confirmations = 6     # sube 5 → 6
min_confirmations_high_price = 7
```

## Datos requeridos

- `market_data.crypto_ohlcv` BTCUSDT 1m con columna `taker_buy_volume` o
  equivalente — **verificar primero**. Si no existe, primer paso es
  extender el adapter de Binance para ingest de trades agregados o usar
  el endpoint `aggTrades` para reconstruir CVD.
- El resto ya disponible (paper_ticks, chainlink feed, macro candles 5m).

## Implementación

_(pendiente — depende de verificación de disponibilidad de CVD)_

- Módulo: `src/trading/strategies/polymarket_btc5m/cvd_confirm_t2_v0.py`
  (heredar de `TrendConfirmT1V1`, override `_compute_confirmations` para
  agregar f8).
- Config: `config/strategies/pbt5m_cvd_confirm_t2_v0.toml`
- Dispatch: registrar en `trading.cli.backtest._load_strategy`.

## Plan de validación

1. Verificar que Binance ingestor ya trae buy/sell volume 1m; si no, extender adapter.
2. Histograma offline de CVD 3m en los trades existentes de trend_confirm_t1_v1
   para calibrar `cvd_min_usd` (elegir percentil 40-60).
3. Backtest 14d vs baseline trend_confirm_t1_v1 en misma ventana.
4. Walk-forward 3x7d si pasa (1).
5. Paper 7d antes de activar.

## Historial

### 2026-04-24 — creación

Hipótesis inicial. Decisión: extender `trend_confirm_t1_v1` en vez de
crear estrategia nueva desde cero, porque el stack de filtros ya es
estable. El único cambio estructural es agregar f8 (CVD) y subir el
umbral de confirmaciones en 1. Falsificación explícita antes de escribir
código.

Bloqueante identificado: disponibilidad de taker-buy volume en Binance
1m en Postgres. Revisar antes de implementar.
