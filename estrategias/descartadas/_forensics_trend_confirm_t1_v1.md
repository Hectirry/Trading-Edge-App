# Forense — `trend_confirm_t1_v1` FAIL del 24-abr (paper_ticks)

**No es una estrategia, es un informe puntual.** Prefijo `_` para que el
INDICE no lo tome por una hipótesis viva.

- Backtest investigado: `21dcdc91-994d-453c-a374-866c1168f4a7`
  (`backtest-2026-04-24-1759.md`, FAIL, win_rate 9.7%, 31 trades, 6 h)
- Backtest contraste: `1833b654-b722-4e96-aa55-bdaaa7cfeca8`
  (`backtest-2026-04-24-2221.md`, MARGINAL, win_rate 76.1%, 590 trades,
  6 días, **fuente = polybot_sqlite**)
- Mismo strategy commit, mismo `params_hash=c5a0d6788af60179`. La única
  diferencia material es `--source paper_ticks` vs `--source polybot_sqlite`.

## TL;DR

**Bug estructural confirmado en la ruta `paper_ticks → backtest_driver`.**
Dos errores compuestos producen settle sistemáticamente sesgado; la
estrategia no es responsable del 9.7% win rate.

1. `src/trading/paper/backtest_loader.py:101–110` — el `ohlcv_open`
   usa `interval='5m'` y consulta el candle cuyo `ts = window_close − 300`.
   Como `crypto_ohlcv.ts` es el **timestamp de apertura del candle**
   (Binance kline `k["t"]`, ver `src/trading/ingest/binance/adapter.py:163`),
   ese candle es el que **cubre la ventana 5 m** y su campo `close` es el
   precio en `window_close`, **no en `window_open`**. Así, `open_price` que
   ve la estrategia ≈ el precio al que el mercado va a resolver, no el
   strike del mercado.
2. `src/trading/engine/backtest_driver.py:99–110` — `_final_price_of`
   toma `last.chainlink_price` truthy. En `paper_ticks` el feed de
   Chainlink se actualiza muy lento (cadencia EAC en Polygon, comentado
   en `tick_recorder.py:76–79`). Durante la ventana 12:00→18:00 UTC del
   23-abr `chainlink_price` se quedó **congelado en cuatro valores** para
   33 markets distintos:

   | rango de markets | chainlink congelado |
   |---|---|
   | 12:05–13:50 | 77 547.35119 |
   | 13:55–15:35 | 77 632.78740 |
   | 15:40–16:25 | 78 471.35411 |
   | 16:30–17:40 | 78 437.11401 |

   El driver usa esos valores como `final_price` para resolver
   `went_up`, contra un `open_price` que ya está corrompido por (1).

`backfill_paper_settles.py` (producción, paper-vs-live) **no** sufre (2)
porque toma `crypto_ohlcv` 1 m al `close_time` exacto del market — es
decir, prod settles en paper son correctos. El bug es exclusivo del
backtest_driver vía paper_ticks.

## Evidencia cuantitativa (recompute desde Postgres, no del run)

`scripts/forensics_trend_fail.py` recompone, para cada uno de los 31
trades del backtest:

- `loader_open` que produce `paper/backtest_loader.py` hoy (5 m close
  en `ts=close-300`).
- `canon_open` = 1 m close en `ts=close-300` (lo que `backfill_paper_settles`
  usa como apertura → ≈ precio en `window_open`).
- `driver_final` = `chainlink_price` del último tick (lo que
  `_final_price_of` devolvería).
- `canon_settle` = 1 m close en `ts=close` (Binance, canónico).
- Compara la resolución almacenada con (a) lo que el driver
  recomputaría hoy y (b) la resolución canónica.

Resultados:

| métrica | valor |
|---|---|
| stored win rate | 3 / 31 = **9.7 %** |
| driver-today vs stored | 28 / 31 match → bug **reproducible** (las 3 discrepancias son markets donde Chainlink se actualizó después del run) |
| canonical (Binance 1 m) win rate | 19 / 31 = **61.3 %** |
| canonical disagrees con stored | 20 / 31 |
| dirección de la estrategia correcta (vista canónica) | 19 / 31 |

Interpretación: si el plumbing fuera correcto, el mismo set de entries
habría rendido **61 %** de aciertos sobre 31 trades. La hipótesis
"trend confirmado por AFML stack" no está falsificada por estos datos.

### Por qué el driver y el canon discrepan tan fuerte

Tomar trade idx 0, market `btc-updown-5m-1776945900` (close 12:05 UTC):

- `loader_open` = 77 768.87 (5 m close at 12:00 = price at 12:05).
- `canon_open` = 77 736.76 (1 m close at 12:00 = price at 12:01,
  proxy del precio al abrir la ventana).
- `driver_final` = 77 547.35 (chainlink congelado).
- `canon_settle` = 77 764.38 (Binance 1 m close at 12:05).

Driver: `went_up = 77 547.35 > 77 768.87` → **False** → YES_UP pierde.
Canon : `went_up = 77 764.38 > 77 736.76` → **True**  → YES_UP gana.

Repetido sobre los 12 primeros markets, todos terminan en loss porque
Chainlink congelado < `loader_open`, mientras que Binance real subió.

### Síntoma colateral: `edge_bps` saturado

`metadata.edge_at_entry` aparece en ~0.5 (5000 bps) en 28 / 31 trades.
Esto **no** indica que la estrategia "vio una oportunidad enorme": el
indicator stack recibe un `ctx.open_price` corrompido y un `vol_ewma`
calculado sobre la trayectoria de `spot_price` cuando reset por market;
con esos inputs `black_scholes_binary_prob` se satura. El edge_bps no
mide nada utilizable mientras (1) y (2) sigan vivos; cualquier filtro
basado en `ctx.edge` o `ctx.model_prob_yes` está midiendo ruido. El
backtest sobre `polybot_sqlite` no exhibe el síntoma porque ahí
`open_price` y `chainlink_price` provienen del mismo recorder
(sibling `polybot-btc5m`) y son consistentes entre sí.

## Reproducción mínima

```sh
docker compose exec tea-engine python scripts/forensics_trend_fail.py
```

Salida bajo `### Summary across 31 trades` confirma los 5 números
de la tabla. Tabla por trade incluida arriba (col `idx … pnl`).

## Recomendación

**Bug confirmado en `paper/backtest_loader.py` + `engine/backtest_driver.py`,
fix propuesto:**

### Fix 1 — `src/trading/paper/backtest_loader.py`

Sustituir el override `5m close en ts=close-300` por el mismo mecanismo
que `backfill_paper_settles._open_price_for`: leer `1m close` en
`ts=close-300`. Eso da el precio al final del primer minuto post-apertura
(≈ precio al abrir, error ≤ 1 bps) y empareja con el path que
producción ya usa para settle. Diff aproximado:

```diff
-                    "AND interval='5m' AND ts = to_timestamp($1)",
-                    window_close_ts - 300,
+                    "AND interval='1m' AND ts = to_timestamp($1)",
+                    window_close_ts - 300,
```

### Fix 2 — `src/trading/engine/backtest_driver.py`

`_final_price_of` no es seguro cuando la fuente es paper_ticks. Dos
caminos posibles, en orden de menor invasividad:

(a) **Mínimo invasivo:** que `paper/backtest_loader.py` exponga, junto
   a `iter_markets`, un `market_outcomes(from_ts, to_ts)` análogo al de
   `PolybotSQLiteLoader.market_outcomes`, calculado con `crypto_ohlcv`
   1 m al `close_time` (la ruta canónica de `backfill_paper_settles`).
   El driver, si el loader expone ese método, lo prefiere sobre
   `_final_price_of`. Polybot path queda intacto.

(b) **Más limpio pero invasivo:** sustituir `_final_price_of`/`_won_market`
   por una sola función `loader.settle_price(slug)` que encapsule el
   knowledge del settle. Requiere refactor de `PolybotSQLiteLoader` y
   afecta a `last_90s_forecaster_v*`/`contest_*` (todas usan el mismo
   driver). No lo recomiendo en este fix.

Voto por (a). Cambio aditivo en el loader + un branch de 4 líneas en
el driver. No toca polybot path.

### Verificación post-fix

Re-correr el mismo backtest (12:00→18:00 UTC del 23-abr, paper_ticks)
debería entregar entre 55 % y 65 % win rate (banda ancha porque son 31
trades). Si cae fuera de esa banda, hay un tercer bug. Si cae dentro:
la estrategia entrega una señal moderadamente positiva sobre 6 h y la
pregunta original ("¿hay alpha en T-180s + AFML stack?") puede pasarse
a la decisión de promotion sobre el run de 6 días con polybot_sqlite
(76.1 %, 590 trades).

## Conclusión

**Bug confirmado. STOP fase 2 hasta confirmación del usuario.**
No promovemos `bb_residual` ni reentrenamos `last_90s_forecaster_v2`
hasta que (1) y (2) estén arregladas y el backtest del 23-abr 12:00–18:00
re-corra sano. Mezclar señal nueva con plumbing roto enmascararía el
bug y contaminaría los métricos de promotion.

## Fix aplicado (2026-04-25)

### Cambios

1. **`src/trading/paper/backtest_loader.py`** — el override de
   `open_price` ahora consulta `interval='1m'` y **lee la columna
   `open`** (no `close`) en `ts = window_close - 300`, de forma que el
   strike es exactamente el precio en `window_open`. Si no existe
   candle 1 m a ese ts (gap de ingest) la función devuelve `None` y el
   caller cae al fallback `paper_ticks.open_price` que ya existía.
   Helper extraído como `_fetch_ohlcv_window_open(conn, window_close_ts)`.
2. **`src/trading/paper/backtest_loader.py`** — nuevo método
   `PaperTicksLoader.market_outcomes(from_ts, to_ts) -> dict[slug, float]`
   que devuelve el precio de settle canónico (Binance 1 m close en el
   minuto floor del `polymarket_markets.close_time`), idéntico al path de
   `scripts/backfill_paper_settles.py::_settle_price_at`. SQL
   factorizado a `_fetch_settle_price_for_slug(conn, slug)`. La clase
   expone el sentinel `provides_settle_prices = True` para el dispatch
   del driver. `backfill_paper_settles.py` no se tocó (regla del PR).
3. **`src/trading/engine/backtest_driver.py`** — al inicio de
   `run_backtest`, si `getattr(loader, "provides_settle_prices", False)`
   es `True`, llama una sola vez a `loader.market_outcomes(from_ts, to_ts)`
   y loggea `backtest.settle_source=loader.market_outcomes`. Para cada
   market, si el slug está en el dict usa ese settle; si falta, *salta el
   market* (no cae a chainlink — explícito en comentario). Si el loader
   no expone la capability (polybot path), comportamiento previo
   intacto. Excepciones de `market_outcomes` se propagan, no se
   silencian.
4. **Tests nuevos** (no se añadieron deps):
   - `tests/unit/paper/test_backtest_loader_open_price.py` (6 casos):
     verifica que el helper lee 1 m + columna `open` en ts=window-300,
     devuelve `None` en gaps, y que `_fetch_settle_price_for_slug`
     replica el mecanismo de `backfill_paper_settles`.
   - `tests/unit/engine/test_backtest_driver_settle.py` (4 casos):
     dispatch por capability (loader con vs sin `provides_settle_prices`),
     skip silencioso cuando falta el slug, y un caso de regresión que
     reproduce la forma estructural del 23-abr (chainlink congelado
     bajo el open canónico) — la ruta legacy da 0/12 wins, la nueva da
     12/12.
   - `pytest tests/unit/paper/test_backtest_loader_open_price.py
     tests/unit/engine/test_backtest_driver_settle.py` → 10/10 pass.

### Re-run 2026-04-23 12:00→18:00 UTC, paper_ticks (mismo TOML)

Backtest_id nuevo: `6c28c362-f3c9-47b8-ba48-8578ca4a77e1`. Driver loggea
`source=loader.market_outcomes, n_settles=149` y emite un único
`backtest.settle_missing` (1 market sin candle 1 m al close → skipped).

| métrica | FAIL (21dcdc91) | nuevo (6c28c362) |
|---|---|---|
| n_trades | 31 | 38 |
| win_rate | **9.7 %** | **63.2 %** |
| total_pnl | -$124.61 | **+$44.10** |
| sharpe / trade | -1.320 | +0.243 |
| mdd (USD) | -$124.61 | -$15.39 |

Comparación 1:1 sobre slugs en común (n=18):

- **side flipped: 11/18** — confirma el componente de sign-flip que el
  loader_open mal indexado producía (delta_bps cambiaba de signo según
  qué referencia se usaba para el strike).
- resolution changed: 15/18.
- same side: 7/18.
- Σ(pnl_new − pnl_old) sobre slugs comunes = +$106.50.
- 20 slugs únicamente en el run nuevo / 13 únicamente en el FAIL — el
  cambio de `delta_bps` mueve qué markets entran al gate.

### Veredicto

**FIX VALIDADO.** Win rate 63.2 % cae limpiamente en la banda esperada
[55 %, 65 %] — no es señal moderada disfrazada de plumbing roto, es
plumbing arreglado mostrando la señal real. La estrategia
`trend_confirm_t1_v1` queda con un dato útil: produce ~63 % de aciertos
sobre 38 trades en 6 h, lo que en términos de PnL no es enorme
(+$44 sobre $190 staked) pero sí monótonamente positivo. La banda
amplia (n=38 es muestra chica; SE ≈ 8 pp) impide concluir más sin un
walk-forward.

Caso forense **cerrado**. Próxima decisión es de Hector: bb_residual
en `last_90s_forecaster_v2` (fase 2 original) ahora puede ejecutarse
sin contaminación de plumbing. Mover este `.md` a
`estrategias/descartadas/` como cierre formal del incidente.

## Historial

### 2026-04-25 — creación
Forense escrita por Claude tras los FAILs `21dcdc91-…` (paper_ticks 6 h)
y MARGINAL `1833b654-…` (polybot_sqlite 6 d). Se confirma sign-flip
de facto en la ruta `paper_ticks → backtest_driver` (combinación de
`loader_open` mal indexado y `_final_price_of` apoyado en chainlink
congelado). Diff propuesto pero **no aplicado**.

### 2026-04-25 — fix aplicado y validado
Aplicados los dos cambios propuestos (1m+open en loader; canonical
settle vía `loader.market_outcomes` en driver, con dispatch por
capability `provides_settle_prices`). Tests nuevos pasan (10/10).
Re-run del backtest 23-abr 12-18 UTC: 38 trades, **win 63.2 %**,
pnl +$44.10. 11/18 slugs comunes flipearon de side, confirmando el
sign-flip subyacente. Caso cerrado; archivo movido a
`descartadas/` como cierre del incidente.
