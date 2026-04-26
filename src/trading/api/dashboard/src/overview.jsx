// Overview module: live trades ticker + heartbeat grid + kill switch + KPIs

function Overview({ strategies, killArmed, setKillArmed, onTogglePause }) {
  const totalPnl24h = strategies.reduce((s, x) => s + (x.status === "running" ? x.pnl_24h : 0), 0);
  const totalPnl7d = strategies.reduce((s, x) => s + x.pnl_7d, 0);
  const totalTrades = strategies.reduce((s, x) => s + x.n_trades_24h, 0);
  const running = strategies.filter(s => s.status === "running").length;

  // Live trades with a rotating "latest" — fake ticker
  const [tradesBuf, setTradesBuf] = React.useState(() => [...LIVE_TRADES]);
  React.useEffect(() => {
    const id = setInterval(() => {
      setTradesBuf(prev => {
        const s = STRATEGIES[Math.floor(Math.random() * STRATEGIES.length)];
        const side = Math.random() > 0.5 ? "BUY" : "SELL";
        const now = new Date();
        const t = now.toTimeString().slice(0, 8);
        const newTrade = {
          t,
          strat: s.id,
          side,
          venue: s.venue,
          sym: s.asset,
          px: (Math.random() * (s.venue === "Polymarket" ? 0.3 : 500) + (s.venue === "Polymarket" ? 0.4 : 94000)).toFixed(s.venue === "Polymarket" ? 4 : 2),
          qty: s.venue === "Polymarket" ? Math.floor(Math.random() * 200 + 50) : +(Math.random() * 0.05).toFixed(3),
          pnl: +((Math.random() - 0.42) * 5).toFixed(2),
        };
        return [newTrade, ...prev].slice(0, 18);
      });
    }, 1800);
    return () => clearInterval(id);
  }, []);

  // Flashing total PnL
  const [displayPnl, setDisplayPnl] = React.useState(totalPnl24h);
  React.useEffect(() => {
    const id = setInterval(() => {
      setDisplayPnl(prev => prev + (Math.random() - 0.45) * 1.2);
    }, 2400);
    return () => clearInterval(id);
  }, []);

  // Mini sparkline for each strategy, rolling
  const [sparks, setSparks] = React.useState(() =>
    Object.fromEntries(STRATEGIES.map(s => [
      s.id,
      Array.from({ length: 30 }, (_, i) => Math.sin(i * 0.4 + s.id.length) * 4 + (i * 0.2) + Math.random() * 2)
    ]))
  );
  React.useEffect(() => {
    const id = setInterval(() => {
      setSparks(prev => {
        const next = { ...prev };
        STRATEGIES.forEach(s => {
          const last = next[s.id][next[s.id].length - 1];
          const nv = last + (Math.random() - (s.pnl_24h > 0 ? 0.4 : 0.5)) * 2;
          next[s.id] = [...next[s.id].slice(1), nv];
        });
        return next;
      });
    }, 1500);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-4">
      {/* Kill switch banner */}
      <div className={`flex items-center justify-between px-4 py-3 border ${killArmed ? "border-rose-500/40 bg-rose-500/5" : "border-slate-800 bg-[#0d1015]"}`}>
        <div className="flex items-center gap-3">
          <div className={`p-2 ${killArmed ? "bg-rose-500/15 text-rose-400" : "bg-emerald-500/10 text-emerald-400"}`}>
            <Icon.kill />
          </div>
          <div>
            <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-slate-400">Master kill switch</div>
            <div className={`font-mono text-sm ${killArmed ? "text-rose-400" : "text-emerald-400"}`}>
              {killArmed ? "ARMED — all strategies halted" : "DISARMED — engine running"}
              <span className="text-slate-500 ml-2" title="Tiempo en el estado actual">· disarmed 17d 4h</span>
            </div>
          </div>
        </div>
        <button
          onClick={() => setKillArmed(!killArmed)}
          className={`font-mono text-[11px] uppercase tracking-wider px-3 py-1.5 border transition-colors ${
            killArmed
              ? "border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10"
              : "border-rose-500/40 text-rose-400 hover:bg-rose-500/10"
          }`}
          title={killArmed ? "Reanudar engine" : "Detener todo"}
        >
          {killArmed ? "▶ disarm" : "■ arm kill switch"}
        </button>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-6 gap-px bg-slate-800/60 border border-slate-800/80">
        <div className="bg-[#0d1015] p-4"><Stat label="PnL 24h" value={<Flash value={displayPnl.toFixed(2)}>{fmt.money(displayPnl)}</Flash>} t={tone.pnl(displayPnl)} sub="paper · $10,000 book" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="PnL 7d"  value={fmt.money(totalPnl7d)} t={tone.pnl(totalPnl7d)} sub="across 6 strategies" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Trades 24h" value={fmt.num(totalTrades)} sub={`avg horizon · ${Math.round(STRATEGIES.reduce((s,x)=>s+x.horizon_s,0)/STRATEGIES.length/60)}m`} /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Win rate" value={fmt.pct(strategies.reduce((s,x)=>s+x.win_rate*x.n_trades_24h,0)/totalTrades, 1)} sub="volume-weighted" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Sharpe (24h)" value="0.47" sub="per-trade, annualized" /></div>
        <div className="bg-[#0d1015] p-4">
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500 mb-1">Engine status</div>
          <div className="flex items-center gap-2">
            <Pulse tone="emerald" />
            <span className="font-mono text-sm text-emerald-400">{running}/6 running</span>
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">uptime 17d 04h 22m</div>
        </div>
      </div>

      {/* Equity chart + live trades */}
      <div className="grid grid-cols-3 gap-4">
        <Panel className="col-span-2">
          <SectionTitle
            right={<span className="font-mono"><Pill tone="emerald" size="xs">LIVE</Pill> <span className="ml-2 text-slate-500">last tick 14:32:18 UTC</span></span>}
            sub="Aggregate paper PnL across 6 strategies"
          >
            Equity curve · 24h
          </SectionTitle>
          <div className="relative">
            <EquityChart points={EQUITY_24H} height={180} />
            <div className="absolute top-0 left-0 text-[10px] font-mono text-slate-500">+$180</div>
            <div className="absolute bottom-4 left-0 text-[10px] font-mono text-slate-500">$0</div>
            <div className="absolute top-0 right-0 text-right">
              <div className="font-mono text-xl text-emerald-400">{fmt.money(totalPnl24h)}</div>
              <div className="text-[10px] text-slate-500 font-mono">24h paper</div>
            </div>
          </div>
          <div className="grid grid-cols-4 gap-4 mt-3 pt-3 border-t border-slate-800/80">
            <div><div className="text-[10px] font-mono text-slate-500 uppercase">Max DD</div><div className="font-mono text-sm text-rose-400">-$14.6</div></div>
            <div><div className="text-[10px] font-mono text-slate-500 uppercase">Peak</div><div className="font-mono text-sm text-slate-300">+$187.4</div></div>
            <div><div className="text-[10px] font-mono text-slate-500 uppercase">Gross traded</div><div className="font-mono text-sm text-slate-300">$48,212</div></div>
            <div><div className="text-[10px] font-mono text-slate-500 uppercase">Fees paid</div><div className="font-mono text-sm text-slate-300">$42.18</div></div>
          </div>
        </Panel>

        {/* Live trades ticker */}
        <Panel padded={false}>
          <div className="p-3 border-b border-slate-800 flex items-center justify-between">
            <div>
              <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-slate-400">Live trades</h3>
              <p className="text-[11px] text-slate-500 mt-0.5">Streaming · todos los venues</p>
            </div>
            <Pulse tone="emerald" />
          </div>
          <div className="font-mono text-[11px] divide-y divide-slate-800/60" style={{ maxHeight: 340, overflow: "hidden" }}>
            {tradesBuf.slice(0, 12).map((tr, i) => (
              <div key={`${tr.t}-${i}`} className={`px-3 py-1.5 grid grid-cols-[52px_1fr_46px_60px] gap-2 items-center ${i === 0 ? "bg-sky-500/5" : ""}`}>
                <span className="text-slate-500">{tr.t.slice(3)}</span>
                <span className="text-slate-300 truncate" title={tr.strat}>{tr.strat.replace(/_v\d+$/, "")}</span>
                <span className={tone.side(tr.side) + " text-[10px]"}>{tr.side}</span>
                <span className={`text-right tabular-nums ${tone.pnl(tr.pnl)}`}>{fmt.money(tr.pnl)}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      {/* Heartbeat grid */}
      <Panel>
        <SectionTitle
          right={<span className="font-mono text-slate-500">polling 500ms · all healthy</span>}
          sub="Per-strategy engine heartbeat, signal cadence, and 24h PnL sparkline"
        >
          Strategy heartbeat
        </SectionTitle>
        <div className="grid grid-cols-3 gap-px bg-slate-800/60">
          {strategies.map(s => {
            const posPnl = s.pnl_24h >= 0;
            const hbOk = s.heartbeat_ms < 500;
            return (
              <div key={s.id} className="bg-[#0d1015] p-3 space-y-2">
                <div className="flex items-start justify-between">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Pulse tone={s.status === "running" ? "emerald" : "amber"} size={5} />
                      <span className="font-mono text-[12px] text-slate-200 truncate">{s.name}</span>
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">{s.venue} · {s.asset}</div>
                  </div>
                  <button
                    onClick={() => onTogglePause(s.id)}
                    className="text-slate-500 hover:text-slate-200 p-1"
                    title={s.status === "running" ? "Pausar" : "Reanudar"}
                  >
                    {s.status === "running" ? <Icon.pause /> : <Icon.play />}
                  </button>
                </div>
                <div className="flex items-end justify-between">
                  <div>
                    <div className={`font-mono text-lg tabular-nums ${tone.pnl(s.pnl_24h)}`}>{fmt.money(s.pnl_24h)}</div>
                    <div className="text-[10px] text-slate-500 font-mono">24h · {s.n_trades_24h} tr</div>
                  </div>
                  <Sparkline points={sparks[s.id]} stroke={posPnl ? "#34d399" : "#fb7185"} width={90} height={26} />
                </div>
                <div className="flex items-center gap-3 text-[10px] font-mono text-slate-500 pt-1 border-t border-slate-800/60">
                  <span>hb <span className={hbOk ? "text-slate-300" : "text-amber-400"}>{s.heartbeat_ms}ms</span></span>
                  <span>·</span>
                  <span>sig {fmt.dur(s.last_signal_s)}</span>
                  <span>·</span>
                  <span>wr {fmt.pct(s.win_rate, 1)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}

Object.assign(window, { Overview });
