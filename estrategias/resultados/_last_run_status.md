# último run VPS

Status: **FAIL**
Timestamp: 2026-04-24T17:52:02Z
Nota: todos los backtests fallaron

## stderr (últimas 20 líneas)

```
From github.com:Hectirry/Trading-Edge-App
 * branch            main       -> FETCH_HEAD
Already on 'main'
From github.com:Hectirry/Trading-Edge-App
 * branch            main       -> FETCH_HEAD
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/home/coder/Trading-Edge-App/src/trading/cli/backtest.py", line 29, in <module>
    from trading.strategies.polymarket_btc5m._macro_provider import Candle, FixedMacroProvider
  File "/home/coder/Trading-Edge-App/src/trading/strategies/polymarket_btc5m/_macro_provider.py", line 23, in <module>
    from trading.engine.features.macro import MacroSnapshot, snapshot
  File "/home/coder/Trading-Edge-App/src/trading/engine/features/__init__.py", line 8, in <module>
    from trading.engine.features import atr, jumps, macro, micro, microprice, mlofi, vpin
ImportError: cannot import name 'atr' from partially initialized module 'trading.engine.features' (most likely due to a circular import) (/home/coder/Trading-Edge-App/src/trading/engine/features/__init__.py)
```
