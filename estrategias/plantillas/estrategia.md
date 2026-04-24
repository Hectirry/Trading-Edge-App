# <nombre>

Estado: `en-desarrollo` | `activa` | `descartada`
Family: `polymarket_btc5m` | `grid` | `<nueva>`
Creada: YYYY-MM-DD
Autor: Hector + Claude

## Hipótesis

Una frase. Qué patrón del mercado explota y por qué debería tener edge.

## Variables clave

- Horizonte / ventana: ej. entrada T-180s, horizonte 5m.
- Señal de dirección: ej. `sign(delta_bps)` con `|delta_bps| >= 1.0`.
- Filtros / gates: lista corta, un bullet cada uno.
- Sizing: fijo / kelly / ticket-scaled.

## Falsificación

Resultado que mataría la hipótesis. Ejemplos: "Sharpe diario < 0 en OOS
sobre 7 días con n ≥ 40 trades", "AUC fold-stable < 0.52". Ser concreto.
Sin esto no se abre la estrategia.

## Parámetros provisionales

Valores iniciales para backtest exploratorio. Van al `[params]` del TOML
cuando se implemente.

```
# ejemplo
entry_horizon_s = 180
delta_bps_min = 1.0
min_confirmations = 5
```

## Datos requeridos

Qué debe existir en Postgres antes de correr backtest. Si falta, el
primer paso es ingest, no código de estrategia.

- `market_data.crypto_ohlcv` BTCUSDT 5m — sí/no, desde cuándo.
- `market_data.paper_ticks` — sí/no.
- Otras fuentes (coinalyze, chainlink direct, etc.).

## Implementación

_(completar cuando exista código)_

- Módulo: `src/trading/strategies/<family>/<nombre>.py`
- Config: `config/strategies/<prefix>_<nombre>.toml`
- Dispatch: `src/trading/cli/backtest.py` (bloque `_load_strategy`)
- Introducido en commit: `<hash corto>`

## Plan de validación

1. Backtest inicial: rango de fechas, fuente, criterios de aceptación.
2. Walk-forward si n ≥ 100 trades.
3. Paper trading mínimo N días antes de activar.

## Historial

### YYYY-MM-DD — creación

Hipótesis inicial. Decisiones de diseño clave (≤5 líneas).
