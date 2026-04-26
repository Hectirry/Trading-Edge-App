// Research module — backtests table, walk-forward, paper-vs-backtest drift

function Research({ backtests }) {
  const [selectedStrat, setSelectedStrat] = React.useState("all");
  const [tab, setTab] = React.useState("backtests");

  const strats = Array.from(new Set(backtests.map(b => b.strategy_name)));
  const filtered = backtests.filter(b => selectedStrat === "all" || b.strategy_name === selectedStrat);

  return (
    <div className="space-y-4">
      {/* Sub-nav */}
      <div className="flex items-center gap-0 border-b border-slate-800">
        {[
          { k: "backtests", label: "Backtests", n: backtests.length },
          { k: "walkforward", label: "Walk-forward", n: WALK_FORWARD.windows.length },
          { k: "drift", label: "Paper vs backtest", n: PAPER_VS_BT.length },
        ].map(t => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`px-4 py-2 font-mono text-[11px] uppercase tracking-wider border-b-2 -mb-px ${
              tab === t.k ? "text-emerald-400 border-emerald-500" : "text-slate-400 border-transparent hover:text-slate-200"
            }`}>
            {t.label} <span className="text-slate-600 ml-1">{t.n}</span>
          </button>
        ))}
      </div>

      {tab === "backtests" && <BacktestsTable backtests={filtered} selectedStrat={selectedStrat} setSelectedStrat={setSelectedStrat} strats={strats} />}
      {tab === "walkforward" && <WalkForward />}
      {tab === "drift" && <Drift />}
    </div>
  );
}

function BacktestsTable({ backtests, selectedStrat, setSelectedStrat, strats }) {
  const [sel, setSel] = React.useState(backtests[0] || null);
  React.useEffect(() => { setSel(backtests[0] || null); }, [backtests.length]);

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="flex items-center gap-3 text-[11px] font-mono">
        <Icon.filter className="text-slate-500" />
        <span className="text-slate-500 uppercase tracking-widest">Strategy</span>
        <div className="flex border border-slate-800">
          <button onClick={() => setSelectedStrat("all")}
            className={`px-2.5 py-1 uppercase tracking-wider ${selectedStrat === "all" ? "bg-emerald-500/10 text-emerald-400" : "text-slate-400 hover:text-slate-200"}`}>
            all
          </button>
          {strats.map(s => (
            <button key={s} onClick={() => setSelectedStrat(s)}
              className={`px-2.5 py-1 uppercase tracking-wider ${selectedStrat === s ? "bg-emerald-500/10 text-emerald-400" : "text-slate-400 hover:text-slate-200"}`}>
              {s}
            </button>
          ))}
        </div>
        <div className="ml-auto text-slate-500">{backtests.length} runs</div>
      </div>

      <div className="grid grid-cols-[1fr_380px] gap-4">
        {/* Table */}
        <Panel padded={false}>
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-[11px]">
              <thead>
                <tr className="border-b border-slate-800 text-slate-500 uppercase tracking-wider text-[10px]">
                  <th className="text-left px-3 py-2">When</th>
                  <th className="text-left px-3 py-2">Strategy</th>
                  <th className="text-right px-3 py-2">Trades</th>
                  <th className="text-right px-3 py-2">PnL</th>
                  <th className="text-right px-3 py-2">Win rate</th>
                  <th className="text-right px-3 py-2">Sharpe</th>
                  <th className="text-right px-3 py-2">MDD</th>
                  <th className="text-center px-3 py-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {backtests.map(b => (
                  <tr key={b.id} onClick={() => setSel(b)}
                    className={`border-b border-slate-800/60 cursor-pointer hover:bg-slate-800/30 ${sel?.id === b.id ? "bg-emerald-500/5" : ""}`}>
                    <td className="px-3 py-1.5 text-slate-400">{b.started_at.slice(5, 16).replace("T", " ")}</td>
                    <td className="px-3 py-1.5 text-slate-200">{b.strategy_name}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-slate-300">{b.n_trades}</td>
                    <td className={`px-3 py-1.5 text-right tabular-nums ${tone.pnl(b.total_pnl)}`}>{fmt.money(b.total_pnl)}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-slate-300">{fmt.pct(b.win_rate, 1)}</td>
                    <td className={`px-3 py-1.5 text-right tabular-nums ${b.sharpe_per_trade >= 0 ? "text-slate-200" : "text-rose-400"}`}>{b.sharpe_per_trade.toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-rose-400">{b.mdd_usd.toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-center">
                      <Pill tone={b.status === "completed" ? "emerald" : "amber"} size="xs">{b.status}</Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        {/* Detail */}
        {sel && (
          <Panel>
            <SectionTitle right={<button className="text-sky-400 font-mono text-[10px] uppercase hover:text-sky-300">open report →</button>}>
              Run detail
            </SectionTitle>
            <div className="space-y-3">
              <div>
                <div className="font-mono text-[12px] text-slate-200">{sel.strategy_name}</div>
                <div className="text-[10px] font-mono text-slate-500 mt-0.5">{sel.id.slice(0, 8)} · dataset {sel.dataset_from.slice(5, 10)} → {sel.dataset_to.slice(5, 10)}</div>
              </div>
              <div className="grid grid-cols-3 gap-3 pt-2 border-t border-slate-800/60">
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">PF</div><div className="font-mono text-sm text-slate-200">{sel.metrics.performance.profit_factor.toFixed(2)}</div></div>
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">Expectancy</div><div className={`font-mono text-sm ${tone.pnl(sel.metrics.performance.expectancy)}`}>{sel.metrics.performance.expectancy.toFixed(3)}</div></div>
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">Fees</div><div className="font-mono text-sm text-slate-200">${sel.metrics.performance.fees_paid.toFixed(2)}</div></div>
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">Avg win</div><div className="font-mono text-sm text-emerald-400">${sel.metrics.performance.avg_win.toFixed(2)}</div></div>
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">Avg loss</div><div className="font-mono text-sm text-rose-400">${sel.metrics.performance.avg_loss.toFixed(2)}</div></div>
                <div><div className="text-[9px] font-mono text-slate-500 uppercase">Calmar</div><div className="font-mono text-sm text-slate-200">{sel.metrics.risk_adjusted.calmar.toFixed(2)}</div></div>
              </div>
              <div className="pt-2 border-t border-slate-800/60">
                <div className="text-[10px] font-mono text-slate-500 uppercase mb-2">By volatility regime</div>
                <div className="space-y-1">
                  {["low", "med", "high"].map(r => {
                    const vr = sel.metrics.by_vol_regime[r];
                    return (
                      <div key={r} className="grid grid-cols-[40px_60px_1fr_70px] font-mono text-[10.5px] items-center">
                        <span className="text-slate-500 uppercase">{r}</span>
                        <span className="text-slate-400">{vr.n_trades} tr</span>
                        <span className="text-slate-400">wr {fmt.pct(vr.win_rate, 1)}</span>
                        <span className={`text-right ${tone.pnl(vr.pnl)}`}>{fmt.money(vr.pnl)}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
              <div className="pt-2 border-t border-slate-800/60 text-[10px] font-mono text-slate-500">
                Took {Math.round((new Date(sel.ended_at) - new Date(sel.started_at)) / 1000)}s · {sel.metrics.exposure.trades_per_day.toFixed(1)} tr/day
              </div>
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function WalkForward() {
  const w = WALK_FORWARD;
  const maxBar = Math.max(...w.windows.map(x => Math.abs(x.oos_pnl))) || 1;
  return (
    <div className="space-y-4">
      <DemoBanner note="Walk-forward usa datos de ejemplo. La tabla research.walk_forward_runs existe pero no está expuesta vía /api/v1." />
      <Panel>
        <SectionTitle
          sub={`Rolling train/test windows · ${w.strategy}`}
          right={<Pill tone="emerald" size="xs">6/7 positive OOS</Pill>}
        >
          Walk-forward analysis
        </SectionTitle>
        <div className="grid grid-cols-7 gap-px bg-slate-800/60">
          {w.windows.map(win => {
            const pos = win.oos_sharpe >= 0;
            const h = (Math.abs(win.oos_pnl) / maxBar) * 80;
            return (
              <div key={win.id} className="bg-[#0d1015] p-2 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] text-slate-300">{win.id}</span>
                  <Pill tone={pos ? "emerald" : "rose"} size="xs">{pos ? "ok" : "fail"}</Pill>
                </div>
                <div className="text-[9px] font-mono text-slate-500 leading-tight">
                  <div>train {win.train.slice(0, 11)}</div>
                  <div>test {win.test.slice(0, 11)}</div>
                </div>
                <div className="h-20 flex items-end justify-center border-t border-slate-800/60 pt-1">
                  <div className={`w-6 ${pos ? "bg-emerald-500/60" : "bg-rose-500/60"}`} style={{ height: h }} />
                </div>
                <div className="font-mono text-[10px] space-y-0.5">
                  <div className="flex justify-between"><span className="text-slate-500">IS</span><span className="text-slate-300">{win.is_sharpe.toFixed(2)}</span></div>
                  <div className="flex justify-between"><span className="text-slate-500">OOS</span><span className={pos ? "text-emerald-400" : "text-rose-400"}>{win.oos_sharpe.toFixed(2)}</span></div>
                  <div className="flex justify-between"><span className="text-slate-500">PnL</span><span className={pos ? "text-emerald-400" : "text-rose-400"}>{fmt.money(win.oos_pnl)}</span></div>
                </div>
              </div>
            );
          })}
        </div>
      </Panel>

      <Panel>
        <SectionTitle>Degradation ratio</SectionTitle>
        <div className="font-mono text-[11px] space-y-2">
          <div className="grid grid-cols-[200px_1fr_80px] items-center gap-3">
            <span className="text-slate-400">Avg IS Sharpe</span>
            <div className="h-2 bg-slate-800"><div className="h-2 bg-sky-500/60" style={{ width: "78%" }} /></div>
            <span className="text-right text-slate-200 tabular-nums">0.77</span>
          </div>
          <div className="grid grid-cols-[200px_1fr_80px] items-center gap-3">
            <span className="text-slate-400">Avg OOS Sharpe</span>
            <div className="h-2 bg-slate-800"><div className="h-2 bg-emerald-500/60" style={{ width: "40%" }} /></div>
            <span className="text-right text-slate-200 tabular-nums">0.40</span>
          </div>
          <div className="grid grid-cols-[200px_1fr_80px] items-center gap-3 pt-2 border-t border-slate-800/60">
            <span className="text-slate-400">Degradation</span>
            <div className="h-2 bg-slate-800"><div className="h-2 bg-amber-500/60" style={{ width: "48%" }} /></div>
            <span className="text-right text-amber-400 tabular-nums">-48%</span>
          </div>
          <div className="text-[10px] text-slate-500 pt-2">Moderate overfit — acceptable for this regime. Ningún window está en pérdida severa.</div>
        </div>
      </Panel>
    </div>
  );
}

function Drift() {
  return (
    <div className="space-y-4">
    <DemoBanner note="Paper-vs-backtest usa datos de ejemplo. La tabla research.paper_vs_backtest_comparisons existe pero no está expuesta vía /api/v1." />
    <Panel>
      <SectionTitle
        sub="Expected (backtest) vs realized (paper) metrics · last 7d"
        right={<span className="font-mono text-[11px] text-slate-500">5 aligned · 1 watch</span>}
      >
        Paper vs backtest drift
      </SectionTitle>
      <div className="overflow-x-auto">
        <table className="w-full font-mono text-[11px]">
          <thead>
            <tr className="border-b border-slate-800 text-slate-500 uppercase tracking-wider text-[10px]">
              <th className="text-left px-3 py-2">Strategy</th>
              <th className="text-right px-3 py-2">BT Sharpe</th>
              <th className="text-right px-3 py-2">Paper Sharpe</th>
              <th className="text-right px-3 py-2">Δ</th>
              <th className="text-right px-3 py-2">BT WR</th>
              <th className="text-right px-3 py-2">Paper WR</th>
              <th className="text-right px-3 py-2">Δ</th>
              <th className="px-3 py-2 text-center">Drift</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {PAPER_VS_BT.map(r => {
              const ds = r.paper_sharpe - r.bt_sharpe;
              const dw = r.paper_winrate - r.bt_winrate;
              return (
                <tr key={r.strat} className="border-b border-slate-800/60 hover:bg-slate-800/30">
                  <td className="px-3 py-2 text-slate-200">{r.strat}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">{r.bt_sharpe.toFixed(2)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-200">{r.paper_sharpe.toFixed(2)}</td>
                  <td className={`px-3 py-2 text-right tabular-nums ${ds >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{ds >= 0 ? "+" : ""}{ds.toFixed(2)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">{fmt.pct(r.bt_winrate, 1)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-200">{fmt.pct(r.paper_winrate, 1)}</td>
                  <td className={`px-3 py-2 text-right tabular-nums ${dw >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{(dw * 100).toFixed(1)}pp</td>
                  <td className="px-3 py-2 text-center">
                    <Pill tone={r.drift === "aligned" ? "emerald" : "amber"} size="xs">{r.drift}</Pill>
                  </td>
                  <td className="px-3 py-2 text-right">
                    {/* diff bar */}
                    <div className="inline-flex items-center h-2 w-24 bg-slate-800 relative">
                      <div className="absolute left-1/2 top-0 bottom-0 w-px bg-slate-600" />
                      <div className={`absolute top-0 bottom-0 ${ds >= 0 ? "bg-emerald-500/60 left-1/2" : "bg-rose-500/60 right-1/2"}`}
                        style={{ width: `${Math.min(Math.abs(ds) * 120, 50)}%` }} />
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
    </div>
  );
}

Object.assign(window, { Research });
