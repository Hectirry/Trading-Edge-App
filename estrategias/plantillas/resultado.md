# backtest — {{strategy_name}}

Fecha corrida: {{started_at}}
Commit: {{strategy_commit}}
Params hash: {{params_hash}}
Ventana datos: {{dataset_from}} → {{dataset_to}}
Fuente: {{data_source}}
Backtest ID: {{backtest_id}}
Reporte HTML: {{report_path}}

## Verdict

`OK` | `MARGINAL` | `FAIL`

Regla (mínima, editable en `scripts/export_result_md.py`):

- OK: `n_trades ≥ 30`, `sharpe_per_trade ≥ 0.10`, `total_pnl > 0`, `mdd_usd > -20`.
- FAIL: `total_pnl < 0` o `sharpe_per_trade < 0`.
- MARGINAL: el resto.

## Métricas

| métrica | valor |
|---|---|
| n_trades | {{n_trades}} |
| win_rate | {{win_rate_pct}}% |
| total_pnl | ${{total_pnl}} |
| sharpe / trade | {{sharpe_per_trade}} |
| sharpe diario | {{sharpe_daily}} |
| mdd (USD) | ${{mdd_usd}} |

## Notas

Texto breve, ≤10 líneas. Lo que este resultado cambió en la hipótesis.
Sólo lo no-obvio a partir de las métricas. Dejar vacío si nada.

## Links para drill-down

- HTML: `{{report_path}}` (trades + gráficos de equity).
- DB: `SELECT * FROM research.backtest_trades WHERE backtest_id = '{{backtest_id}}';`
- Grafana: `/grafana` → dashboard "backtests" → fila con este `started_at`.
