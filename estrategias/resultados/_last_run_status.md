# último run VPS

Status: **FAIL**
Timestamp: 2026-04-24T15:51:37Z
Nota: todos los backtests fallaron

## stderr (últimas 20 líneas)

```
+ '[' 0 -eq 0 ']'
+ write_status FAIL 'todos los backtests fallaron'
+ local status=FAIL
+ local 'note=todos los backtests fallaron'
+ local ts
++ date -u +%Y-%m-%dT%H:%M:%SZ
+ ts=2026-04-24T15:51:37Z
++ dirname /home/coder/Trading-Edge-App/estrategias/resultados/_last_run_status.md
+ mkdir -p /home/coder/Trading-Edge-App/estrategias/resultados
+ echo '# último run VPS'
+ echo ''
+ echo 'Status: **FAIL**'
+ echo 'Timestamp: 2026-04-24T15:51:37Z'
+ echo 'Nota: todos los backtests fallaron'
+ echo ''
+ '[' FAIL '!=' OK ']'
+ echo '## stderr (últimas 20 líneas)'
+ echo ''
+ echo '```'
+ tail -n 20 /tmp/tea-vps-daily-stderr.WPlpi1
```
