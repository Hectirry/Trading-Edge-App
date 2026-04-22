"""Report generator. Writes an HTML file under
src/trading/research/reports/ and upserts rows into research.backtests +
research.backtest_trades. See ADR 0003 for the extended columns."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import plotly.graph_objects as go
from jinja2 import Template

from trading.common.logging import get_logger
from trading.engine.backtest_driver import BacktestRunResult, BacktestTrade, compute_kpis

log = get_logger(__name__)

REPORTS_DIR = Path("src/trading/research/reports")

NAUTILUS_VERSION_NOTE = "nautilus_trader==1.215.0 pinned, not loaded in Phase 2 (ADR 0006)"


def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip()
    except Exception:
        return "unknown"


def _params_hash(params: dict) -> str:
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _edge_bucket(bps: float) -> str:
    a = abs(bps)
    if a < 300:
        return "below_300"
    if a < 500:
        return "300-500"
    if a < 800:
        return "500-800"
    return "800+"


def _t_in_window_bucket(t: float) -> str:
    if t < 90:
        return "60-90"
    if t < 120:
        return "90-120"
    if t < 150:
        return "120-150"
    if t < 180:
        return "150-180"
    if t < 210:
        return "180-210"
    return "210-240"


def compute_segmentation(trades: list[BacktestTrade]) -> dict:
    def agg(rows: list[BacktestTrade]) -> dict:
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}
        wins = sum(1 for r in rows if r.resolution == "win")
        pnl = sum(r.pnl_usd for r in rows)
        return {"n": n, "win_rate": wins / n, "avg_pnl": pnl / n, "total_pnl": pnl}

    by_hour: dict[int, list[BacktestTrade]] = defaultdict(list)
    by_side: dict[str, list[BacktestTrade]] = defaultdict(list)
    by_edge_bps: dict[str, list[BacktestTrade]] = defaultdict(list)
    by_t_in_window: dict[str, list[BacktestTrade]] = defaultdict(list)
    by_vol_regime: dict[str, list[BacktestTrade]] = defaultdict(list)
    for t in trades:
        hour = datetime.fromtimestamp(t.entry_ts, tz=UTC).hour
        by_hour[hour].append(t)
        by_side[t.side].append(t)
        by_edge_bps[_edge_bucket(t.edge_at_entry * 10_000)].append(t)
        by_t_in_window[_t_in_window_bucket(t.entry_t_in_window)].append(t)
        by_vol_regime[t.vol_regime_at_entry].append(t)

    return {
        "by_hour_utc": {str(h): agg(rs) for h, rs in sorted(by_hour.items())},
        "by_side": {s: agg(rs) for s, rs in by_side.items()},
        "by_edge_bps": {k: agg(rs) for k, rs in by_edge_bps.items()},
        "by_t_in_window": {k: agg(rs) for k, rs in by_t_in_window.items()},
        "by_vol_regime": {k: agg(rs) for k, rs in by_vol_regime.items()},
    }


def _equity_curve_figure(trades: list[BacktestTrade]) -> str:
    if not trades:
        return "<p>No trades.</p>"
    xs = [datetime.fromtimestamp(t.exit_ts, tz=UTC) for t in trades]
    eq = 0.0
    ys = []
    for t in trades:
        eq += t.pnl_usd
        ys.append(eq)
    fig = go.Figure(go.Scatter(x=xs, y=ys, mode="lines", name="Equity"))
    fig.update_layout(
        title="Equity curve (cumulative PnL, USD)",
        xaxis_title="time (UTC)",
        yaxis_title="equity (USD)",
        height=380,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def _pnl_histogram_figure(trades: list[BacktestTrade]) -> str:
    if not trades:
        return ""
    xs = [t.pnl_usd for t in trades]
    fig = go.Figure(go.Histogram(x=xs, nbinsx=30, name="PnL"))
    fig.update_layout(
        title="PnL distribution (USD per trade)",
        xaxis_title="pnl (USD)",
        yaxis_title="count",
        height=320,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


HTML_TEMPLATE = Template(
    """<!doctype html>
<html><head><meta charset="utf-8">
<title>{{ strategy }} — {{ dataset_from }} → {{ dataset_to }}</title>
<style>
body{font-family:-apple-system,sans-serif;margin:24px;max-width:1200px;color:#222}
h1{border-bottom:2px solid #333;padding-bottom:6px}
h2{margin-top:28px;color:#444}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:12px 0}
.kpi{background:#f6f6f6;padding:10px 14px;border-radius:6px}
.kpi b{display:block;font-size:0.85em;color:#666;text-transform:uppercase}
.kpi span{font-size:1.6em;font-weight:600}
table{border-collapse:collapse;font-size:0.92em;margin:8px 0 18px}
th,td{border:1px solid #ddd;padding:4px 8px;text-align:right}
th{background:#eee}
code{background:#f6f6f6;padding:2px 5px;border-radius:3px;font-size:0.9em}
.warn{background:#fff3cd;border-left:4px solid #f5c518;padding:10px;margin:14px 0}
</style></head><body>
<h1>{{ strategy }}</h1>
<p>
  <code>backtest_id</code>: {{ backtest_id }}<br>
  <code>dataset</code>: {{ dataset_from }} → {{ dataset_to }} ({{ duration_days }} days)<br>
  <code>commit</code>: {{ commit }}<br>
  <code>params_hash</code>: {{ params_hash }}<br>
  <code>data_source</code>: {{ data_source }}<br>
  <code>engine</code>: {{ nautilus_note }}
</p>

<div class="warn">
  <b>Sharpe reality check.</b> Annualized Sharpe under i.i.d. trade assumption
  is reported for continuity with the legacy metric, but the trades cluster
  intraday and are not i.i.d. The <i>daily-PnL Sharpe</i> below is the more
  trustworthy risk-adjusted metric. See ADR 0006 and the Phase 2 Sharpe
  audit note in the runbook.
</div>

<h2>KPIs</h2>
<div class="kpi-grid">
  <div class="kpi"><b>trades</b><span>{{ perf.n_trades }}</span></div>
  <div class="kpi"><b>win rate</b><span>{{ "%.1f%%"|format(perf.win_rate*100) }}</span></div>
  <div class="kpi"><b>total pnl</b><span>${{ "%.2f"|format(perf.total_pnl) }}</span></div>
  <div class="kpi"><b>avg pnl</b><span>${{ "%.2f"|format(perf.avg_pnl) }}</span></div>
  <div class="kpi"><b>sharpe/trade</b><span>{{ "%.2f"|format(risk.sharpe_per_trade) }}</span></div>
  <div class="kpi"><b>sharpe ann. iid</b>
    <span>{{ "%.1f"|format(risk.sharpe_annualized_iid) }}</span></div>
  <div class="kpi"><b>sharpe daily</b><span>{{ "%.2f"|format(risk.sharpe_daily) }}</span></div>
  <div class="kpi"><b>max dd</b><span>${{ "%.2f"|format(risk.mdd_usd) }}</span></div>
</div>

<h2>Equity curve</h2>
{{ equity_html | safe }}

<h2>PnL distribution</h2>
{{ pnl_hist_html | safe }}

<h2>Segmentation</h2>
{% for title, rows in segmentation_tables.items() %}
  <h3>{{ title }}</h3>
  <table><tr><th>bucket</th><th>n</th><th>win rate</th><th>avg pnl</th><th>total pnl</th></tr>
  {% for k, v in rows.items() %}
    <tr><td style="text-align:left">{{ k }}</td><td>{{ v.n }}</td>
        <td>{{ "%.1f%%"|format(v.win_rate*100) }}</td>
        <td>${{ "%.2f"|format(v.avg_pnl) }}</td>
        <td>${{ "%.2f"|format(v.total_pnl) }}</td></tr>
  {% endfor %}
  </table>
{% endfor %}

<h2>Trade log (last 50)</h2>
<table><tr><th>idx</th><th>slug</th><th>side</th><th>entry</th><th>exit</th><th>pnl</th><th>res</th></tr>
{% for t in trades_tail %}
  <tr><td>{{ t.trade_idx }}</td><td style="text-align:left">{{ t.market_slug }}</td>
      <td>{{ t.side }}</td>
      <td>{{ "%.4f"|format(t.entry_price) }}</td>
      <td>{{ "%.2f"|format(t.exit_price) }}</td>
      <td>${{ "%.2f"|format(t.pnl_usd) }}</td>
      <td>{{ t.resolution }}</td></tr>
{% endfor %}
</table>

</body></html>
"""
)


async def persist_and_render(
    result: BacktestRunResult,
    dsn: str,
    strategy_name: str,
    params: dict,
    data_source: str,
    output_dir: Path | None = None,
) -> tuple[str, Path]:
    """Insert into research.backtests + research.backtest_trades, render HTML."""
    out_dir = output_dir or REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    backtest_id = str(uuid.uuid4())
    commit = _git_commit()
    phash = _params_hash(params)
    started = datetime.fromtimestamp(result.start_ts, tz=UTC)
    ended = datetime.fromtimestamp(result.end_ts, tz=UTC)
    duration_s = result.end_ts - result.start_ts

    kpis = compute_kpis(result.trades, duration_s)
    segmentation = compute_segmentation(result.trades)

    equity_html = _equity_curve_figure(result.trades)
    pnl_hist_html = _pnl_histogram_figure(result.trades)

    segmentation_tables = {
        "by_hour_utc": segmentation["by_hour_utc"],
        "by_side": segmentation["by_side"],
        "by_edge_bps": segmentation["by_edge_bps"],
        "by_t_in_window": segmentation["by_t_in_window"],
        "by_vol_regime": segmentation["by_vol_regime"],
    }

    html = HTML_TEMPLATE.render(
        strategy=strategy_name,
        backtest_id=backtest_id,
        dataset_from=started.isoformat(),
        dataset_to=ended.isoformat(),
        duration_days=f"{duration_s/86400:.2f}",
        commit=commit,
        params_hash=phash,
        data_source=data_source,
        nautilus_note=NAUTILUS_VERSION_NOTE,
        perf=kpis["performance"],
        risk=kpis["risk_adjusted"],
        equity_html=equity_html,
        pnl_hist_html=pnl_hist_html,
        segmentation_tables=segmentation_tables,
        trades_tail=result.trades[-50:],
    )

    ts_str = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{ts_str}_{strategy_name.replace('/', '_')}_{phash}.html"
    out_path.write_text(html)
    log.info("report.written", path=str(out_path))

    # Persist to DB.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO research.backtests
                    (id, strategy_name, strategy_commit, params_hash, params,
                     dataset_from, dataset_to, started_at, ended_at, status,
                     metrics, report_path, nautilus_version, data_source)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14)
                """,
                backtest_id,
                strategy_name,
                commit,
                phash,
                json.dumps(params),
                started,
                ended,
                datetime.now(tz=UTC),
                datetime.now(tz=UTC),
                "completed",
                json.dumps({**kpis, "segmentation": segmentation}),
                str(out_path),
                NAUTILUS_VERSION_NOTE,
                data_source,
            )
            if result.trades:
                args = [
                    (
                        backtest_id,
                        t.trade_idx,
                        t.market_slug,
                        "BUY",
                        t.stake_usd / max(t.entry_price, 1e-9),
                        datetime.fromtimestamp(t.entry_ts, tz=UTC),
                        t.entry_price,
                        datetime.fromtimestamp(t.exit_ts, tz=UTC),
                        t.exit_price,
                        t.pnl_usd,
                        t.fee,
                        t.side,
                        t.slippage,
                        int(t.entry_t_in_window),
                        t.vol_regime_at_entry,
                        int(t.edge_at_entry * 10_000),
                        json.dumps(asdict(t)),
                    )
                    for t in result.trades
                ]
                await conn.executemany(
                    """
                    INSERT INTO research.backtest_trades
                        (backtest_id, trade_idx, instrument, side, qty,
                         entry_ts, entry_price, exit_ts, exit_price, pnl, fees,
                         strategy_side, slippage, t_in_window_s, vol_regime,
                         edge_bps, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
                    """,
                    args,
                )
    finally:
        await conn.close()

    return backtest_id, out_path
