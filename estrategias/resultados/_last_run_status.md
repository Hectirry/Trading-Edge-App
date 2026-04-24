# último run VPS

Status: **FAIL**
Timestamp: 2026-04-24T14:55:36Z
Nota: git pull --rebase failed

## stderr (últimas 20 líneas)

```
+ printf '[%s] %s\n' 2026-04-24T14:55:36Z 'FAIL: git pull --rebase failed'
+ write_status FAIL 'git pull --rebase failed'
+ local status=FAIL
+ local 'note=git pull --rebase failed'
+ local ts
++ date -u +%Y-%m-%dT%H:%M:%SZ
+ ts=2026-04-24T14:55:36Z
++ dirname /home/coder/Trading-Edge-App/estrategias/resultados/_last_run_status.md
+ mkdir -p /home/coder/Trading-Edge-App/estrategias/resultados
+ echo '# último run VPS'
+ echo ''
+ echo 'Status: **FAIL**'
+ echo 'Timestamp: 2026-04-24T14:55:36Z'
+ echo 'Nota: git pull --rebase failed'
+ echo ''
+ '[' FAIL '!=' OK ']'
+ echo '## stderr (últimas 20 líneas)'
+ echo ''
+ echo '```'
+ tail -n 20 /tmp/tea-vps-daily-stderr.bYMLQI
```
