# último run VPS

Status: **FAIL**
Timestamp: 2026-04-24T15:25:24Z
Nota: todos los backtests fallaron

## stderr (últimas 20 líneas)

```
From github.com:Hectirry/Trading-Edge-App
 * branch            main       -> FETCH_HEAD
Already on 'main'
From github.com:Hectirry/Trading-Edge-App
 * branch            main       -> FETCH_HEAD
Exception in thread Thread-167 (_worker):
Traceback (most recent call last):
  File "/home/coder/Trading-Edge-App/.venv/lib/python3.12/site-packages/asyncpg/connection.py", line 2421, in connect
/home/coder/Trading-Edge-App/scripts/vps_daily.sh: line 177: 703680 Killed                  python -m trading.cli.backtest --strategy "$strategy" --params "$params" --from "$from_ts" --to "$to_ts" --source "$source"
```
