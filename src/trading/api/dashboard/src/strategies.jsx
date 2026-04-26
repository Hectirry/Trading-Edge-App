// Strategies module — expanded cards with full metrics + pause/resume

function Strategies({ strategies, onTogglePause }) {
  const [sortBy, setSortBy] = React.useState("pnl_24h");
  const [filter, setFilter] = React.useState("all");
  const [venueFilter, setVenueFilter] = React.useState("all");

  const venues = ["all", ...Array.from(new Set(strategies.map(s => s.venue)))];

  const filtered = strategies
    .filter(s => filter === "all" || s.status === filter)
    .filter(s => venueFilter === "all" || s.venue === venueFilter)
    .sort((a, b) => (b[sortBy] ?? 0) - (a[sortBy] ?? 0));

  // Per-strategy live mini chart
  const [sparks, setSparks] = React.useState(() =>
    Object.fromEntries(STRATEGIES.map(s => [
      s.id,
      Array.from({ length: 40 }, (_, i) => Math.sin(i * 0.3 + s.id.length) * 6 + (i * 0.3 * (s.pnl_24h > 0 ? 1 : -1)) + Math.random() * 3)
    ]))
  );
  React.useEffect(() => {
    const id = setInterval(() => {
      setSparks(prev => {
        const next = { ...prev };
        STRATEGIES.forEach(s => {
          const last = next[s.id][next[s.id].length - 1];
          const drift = s.pnl_24h > 0 ? -0.4 : -0.52;
          const nv = last + (Math.random() - drift) * 2;
          next[s.id] = [...next[s.id].slice(1), nv];
        });
        return next;
      });
    }, 2000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="flex items-center gap-3 text-[11px] font-mono">
        <Icon.filter className="text-slate-500" />
        <span className="text-slate-500 uppercase tracking-widest">Filters</span>
        <div className="flex border border-slate-800">
          {["all", "running", "paused"].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`px-2.5 py-1 uppercase tracking-wider ${filter === f ? "bg-emerald-500/10 text-emerald-400" : "text-slate-400 hover:text-slate-200"}`}>
              {f}
            </button>
          ))}
        </div>
        <div className="flex border border-slate-800">
          {venues.map(v => (
            <button key={v} onClick={() => setVenueFilter(v)}
              className={`px-2.5 py-1 uppercase tracking-wider ${venueFilter === v ? "bg-emerald-500/10 text-emerald-400" : "text-slate-400 hover:text-slate-200"}`}>
              {v}
            </button>
          ))}
        </div>
        <span className="text-slate-500">·</span>
        <span className="text-slate-500 uppercase tracking-widest">Sort</span>
        <select value={sortBy} onChange={e => setSortBy(e.target.value)}
          className="bg-[#0d1015] border border-slate-800 text-slate-300 px-2 py-1 uppercase tracking-wider">
          <option value="pnl_24h">PnL 24h</option>
          <option value="pnl_7d">PnL 7d</option>
          <option value="sharpe">Sharpe</option>
          <option value="win_rate">Win rate</option>
          <option value="n_trades_24h">Trades</option>
        </select>
        <div className="ml-auto text-slate-500">{filtered.length} of {strategies.length} strategies</div>
      </div>

      {/* Strategy cards */}
      <div className="grid grid-cols-2 gap-4">
        {filtered.map(s => {
          const pos = s.pnl_24h >= 0;
          return (
            <Panel key={s.id}>
              {/* Header */}
              <div className="flex items-start justify-between pb-3 border-b border-slate-800/80">
                <div className="flex items-center gap-3 min-w-0">
                  <Pulse tone={s.status === "running" ? "emerald" : "amber"} />
                  <div className="min-w-0">
                    <div className="font-mono text-[13px] text-slate-100">{s.name}</div>
                    <div className="text-[11px] text-slate-500 font-mono mt-0.5">
                      {s.venue} · {s.asset} · horizon {fmt.sec(s.horizon_s)}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Pill tone={s.status === "running" ? "emerald" : "amber"} size="xs">{s.status}</Pill>
                  <button
                    onClick={() => onTogglePause(s.id)}
                    className={`flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors ${
                      s.status === "running"
                        ? "border-slate-700 text-slate-300 hover:border-amber-500/40 hover:text-amber-400"
                        : "border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10"
                    }`}
                    title={s.status === "running" ? "Pausar estrategia" : "Reanudar estrategia"}
                  >
                    {s.status === "running" ? <><Icon.pause /> pause</> : <><Icon.play /> resume</>}
                  </button>
                </div>
              </div>

              {/* Main metrics */}
              <div className="pt-3">
                <div className="grid grid-cols-3 gap-x-4 gap-y-3">
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">PnL 24h</div>
                    <div className={`font-mono text-[17px] tabular-nums ${tone.pnl(s.pnl_24h)}`}>{fmt.money(s.pnl_24h)}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">PnL 7d</div>
                    <div className={`font-mono text-[17px] tabular-nums ${tone.pnl(s.pnl_7d)}`}>{fmt.money(s.pnl_7d)}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">Sharpe</div>
                    <div className="font-mono text-[17px] tabular-nums text-slate-200">{s.sharpe.toFixed(2)}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">Win rate</div>
                    <div className="font-mono text-[13px] tabular-nums text-slate-200">{fmt.pct(s.win_rate, 1)}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">Trades 24h</div>
                    <div className="font-mono text-[13px] tabular-nums text-slate-200">{s.n_trades_24h}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[9px] font-mono text-slate-500 uppercase tracking-wider">MDD</div>
                    <div className="font-mono text-[13px] tabular-nums text-rose-400">{fmt.moneyPlain(s.mdd)}</div>
                  </div>
                </div>
                <div className="mt-3">
                  <Sparkline points={sparks[s.id]} stroke={pos ? "#34d399" : "#fb7185"} width={520} height={44} fill={true} />
                </div>
              </div>

              {/* Live status bar */}
              <div className="mt-3 pt-3 border-t border-slate-800/60 grid grid-cols-4 gap-2 text-[10px] font-mono">
                <div>
                  <div className="text-slate-500 uppercase tracking-wider">Heartbeat</div>
                  <div className="text-slate-300">{s.heartbeat_ms}ms <span className="text-emerald-400">●</span></div>
                </div>
                <div>
                  <div className="text-slate-500 uppercase tracking-wider">Last signal</div>
                  <div className="text-slate-300">{fmt.dur(s.last_signal_s)}</div>
                </div>
                <div>
                  <div className="text-slate-500 uppercase tracking-wider">Paper book</div>
                  <div className="text-slate-300">$10,000</div>
                </div>
                <div className="text-right">
                  <button className="text-sky-400 hover:text-sky-300 uppercase tracking-wider">
                    details →
                  </button>
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}

Object.assign(window, { Strategies });
