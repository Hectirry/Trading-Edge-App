// Remaining modules: LLM Copilot, Data Freshness, System Health, Contest A/B

// ---------- LLM Copilot ----------
function Copilot() {
  const [messages, setMessages] = React.useState([]);
  const [input, setInput] = React.useState("");
  const [thinking, setThinking] = React.useState(false);
  // Stable session id per browser tab — keeps server-side conversation history.
  const sessionRef = React.useRef(`web-${Math.random().toString(36).slice(2, 12)}`);

  const totalCost = LLM_COSTS_7D.reduce((s, d) => s + d.cost, 0);
  const totalTokens = LLM_COSTS_7D.reduce((s, d) => s + d.tokens, 0);
  const totalCalls = LLM_COSTS_7D.reduce((s, d) => s + d.calls, 0);

  const suggested = [
    "¿Por qué trend_confirm_t1_v1 underperformed esta semana?",
    "Compare last_90s_forecaster_v3 backtest vs paper",
    "Which strategy has the worst drawdown last 7 days?",
    "Resumen de los últimos 5 backtests",
  ];

  const send = async (txt) => {
    if (!txt.trim() || thinking) return;
    const now = new Date().toTimeString().slice(0, 8);
    setMessages(m => [...m, { role: "user", t: now, content: txt }]);
    setInput("");
    setThinking(true);
    try {
      const res = await window.API.chat({ session_id: sessionRef.current, message: txt });
      setMessages(m => [...m, {
        role: "assistant",
        t: new Date().toTimeString().slice(0, 8),
        content: res.assistant,
        cost: res.cost_usd_this_turn || 0,
        tokens: (res.tokens_in_total || 0) + (res.tokens_out_total || 0),
        model: res.model,
      }]);
    } catch (e) {
      setMessages(m => [...m, {
        role: "assistant",
        t: new Date().toTimeString().slice(0, 8),
        content: `error: ${e.message}`,
      }]);
    } finally {
      setThinking(false);
    }
  };

  return (
    <div className="grid grid-cols-[1fr_320px] gap-4 h-[calc(100vh-140px)]">
      {/* Chat */}
      <Panel padded={false} className="flex flex-col">
        <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between">
          <div>
            <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-slate-400">Copilot · Strategy Analyst</h3>
            <p className="text-[11px] text-slate-500 mt-0.5">claude-haiku-4-5 · grounded on postgres + backtest reports</p>
          </div>
          <Pill tone="emerald" size="xs"><Pulse size={4} /> ready</Pill>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4 font-mono text-[12px]">
          {messages.map((m, i) => (
            <div key={i} className={m.role === "user" ? "" : "bg-slate-800/20 border border-slate-800/60 p-3"}>
              <div className="flex items-center gap-2 mb-1.5">
                <span className={`text-[10px] uppercase tracking-wider ${m.role === "user" ? "text-sky-400" : "text-emerald-400"}`}>
                  {m.role === "user" ? "you" : "copilot"}
                </span>
                <span className="text-[10px] text-slate-500">{m.t}</span>
                {m.model && <span className="text-[10px] text-slate-600 ml-auto">{m.model} · {m.tokens} tok · ${m.cost.toFixed(4)}</span>}
              </div>
              <div className="text-slate-200 whitespace-pre-wrap leading-relaxed">{m.content}</div>
            </div>
          ))}
          {thinking && (
            <div className="bg-slate-800/20 border border-slate-800/60 p-3">
              <div className="text-[10px] uppercase tracking-wider text-emerald-400 mb-1.5">copilot</div>
              <div className="flex gap-1 text-emerald-400">
                <span className="animate-pulse">▊</span>
                <span className="animate-pulse" style={{ animationDelay: "0.2s" }}>▊</span>
                <span className="animate-pulse" style={{ animationDelay: "0.4s" }}>▊</span>
              </div>
            </div>
          )}
        </div>

        {/* Suggested */}
        <div className="px-4 py-2 border-t border-slate-800 flex flex-wrap gap-1.5">
          {suggested.map(s => (
            <button key={s} onClick={() => send(s)}
              className="text-[10.5px] font-mono text-slate-400 border border-slate-800 px-2 py-1 hover:border-emerald-500/40 hover:text-emerald-400">
              {s}
            </button>
          ))}
        </div>

        {/* Input */}
        <div className="p-3 border-t border-slate-800 flex gap-2">
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && send(input)}
            placeholder="Ask about strategies, backtests, drawdowns…"
            className="flex-1 bg-[#0a0b0d] border border-slate-800 px-3 py-2 font-mono text-[12px] text-slate-200 focus:outline-none focus:border-emerald-500/40"
          />
          <button onClick={() => send(input)}
            className="px-3 py-2 border border-emerald-500/40 text-emerald-400 font-mono text-[11px] uppercase hover:bg-emerald-500/10 flex items-center gap-2">
            <Icon.send /> send
          </button>
        </div>
      </Panel>

      {/* Cost tracking sidebar */}
      <div className="space-y-4 overflow-y-auto">
        <Panel>
          <SectionTitle sub="Last 7 days · all queries">Cost tracking</SectionTitle>
          <div className="grid grid-cols-3 gap-3 mb-3">
            <div><div className="text-[9px] font-mono text-slate-500 uppercase">Spend</div><div className="font-mono text-lg text-slate-200">${totalCost.toFixed(2)}</div></div>
            <div><div className="text-[9px] font-mono text-slate-500 uppercase">Tokens</div><div className="font-mono text-lg text-slate-200">{(totalTokens / 1000).toFixed(0)}k</div></div>
            <div><div className="text-[9px] font-mono text-slate-500 uppercase">Calls</div><div className="font-mono text-lg text-slate-200">{totalCalls}</div></div>
          </div>
          <div className="space-y-1">
            {LLM_COSTS_7D.map(d => {
              const max = Math.max(...LLM_COSTS_7D.map(x => x.cost));
              return (
                <div key={d.day} className="grid grid-cols-[44px_1fr_44px] items-center gap-2 font-mono text-[10.5px]">
                  <span className="text-slate-500">{d.day}</span>
                  <div className="h-2 bg-slate-800"><div className="h-2 bg-violet-500/60" style={{ width: `${(d.cost / max) * 100}%` }} /></div>
                  <span className="text-right text-slate-300 tabular-nums">${d.cost.toFixed(2)}</span>
                </div>
              );
            })}
          </div>
          <div className="mt-3 pt-3 border-t border-slate-800/60 text-[10px] font-mono text-slate-500">
            Monthly budget: $120 · <span className="text-emerald-400">$19.20 used (16%)</span>
          </div>
        </Panel>

        <Panel>
          <SectionTitle>Tools available</SectionTitle>
          <div className="space-y-1.5 font-mono text-[11px]">
            {[
              ["query_backtests", "postgres"],
              ["fetch_live_metrics", "engine"],
              ["explain_drawdown", "analysis"],
              ["queue_backtest", "research"],
              ["read_logs", "observability"],
            ].map(([name, tag]) => (
              <div key={name} className="flex items-center justify-between">
                <span className="text-slate-300">{name}</span>
                <span className="text-[9px] text-slate-500 uppercase">{tag}</span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel>
          <SectionTitle>Recent queries</SectionTitle>
          <div className="space-y-2 font-mono text-[11px]">
            {[
              { t: "14:22", q: "why did vwap_revert spike today?" },
              { t: "13:08", q: "compare last 3 runs of trend_confirm" },
              { t: "11:44", q: "oracle lag sniper fill rate" },
              { t: "09:12", q: "explain drawdown on 04-18" },
            ].map((q, i) => (
              <div key={i} className="border-l-2 border-slate-800 pl-2">
                <div className="text-[9px] text-slate-500">{q.t}</div>
                <div className="text-slate-400 truncate">{q.q}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------- Data Freshness ----------
function DataFreshness() {
  // No server endpoint yet — keep mock and label as DEMO.
  const [feeds, setFeeds] = React.useState(DATA_FEEDS);
  React.useEffect(() => {
    const id = setInterval(() => {
      setFeeds(prev => prev.map(f => ({
        ...f,
        lag_ms: Math.max(5, f.lag_ms + (Math.random() - 0.5) * 10),
        msgs_s: Math.max(0.01, f.msgs_s * (0.92 + Math.random() * 0.16)),
      })));
    }, 1500);
    return () => clearInterval(id);
  }, []);

  const ok = feeds.filter(f => f.status === "ok").length;
  const slow = feeds.filter(f => f.status === "slow").length;

  return (
    <div className="space-y-4">
      <DemoBanner note="Data freshness usa datos de ejemplo. No hay endpoint live para feeds todavía." />
      <div className="grid grid-cols-4 gap-px bg-slate-800/60 border border-slate-800/80">
        <div className="bg-[#0d1015] p-4"><Stat label="Feeds healthy" value={<span className="text-emerald-400">{ok}/{feeds.length}</span>} sub="msg/s > threshold" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Feeds degraded" value={<span className={slow > 0 ? "text-amber-400" : "text-slate-400"}>{slow}</span>} sub="oracle chainlink" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Total msg/s" value={Math.round(feeds.reduce((s, f) => s + f.msgs_s, 0)).toLocaleString()} sub="ingest rate" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Avg lag" value={`${Math.round(feeds.reduce((s, f) => s + f.lag_ms, 0) / feeds.length)}ms`} sub="venue → engine" /></div>
      </div>

      <Panel padded={false}>
        <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between">
          <SectionTitle>Feed health · live</SectionTitle>
          <div className="font-mono text-[10px] text-slate-500">refresh 1.5s <Pulse size={4} /></div>
        </div>
        <table className="w-full font-mono text-[11px]">
          <thead>
            <tr className="border-b border-slate-800 text-slate-500 uppercase tracking-wider text-[10px]">
              <th className="text-left px-3 py-2">Feed</th>
              <th className="text-left px-3 py-2">Venue</th>
              <th className="text-right px-3 py-2">Lag</th>
              <th className="text-right px-3 py-2">Msg/s</th>
              <th className="text-right px-3 py-2">Staleness</th>
              <th className="px-3 py-2 text-center">Status</th>
              <th className="px-3 py-2 w-32">Throughput</th>
            </tr>
          </thead>
          <tbody>
            {feeds.map(f => {
              const barW = Math.min(100, (f.msgs_s / 3500) * 100);
              return (
                <tr key={f.name} className="border-b border-slate-800/60 hover:bg-slate-800/20">
                  <td className="px-3 py-2 text-slate-200">{f.name}</td>
                  <td className="px-3 py-2 text-slate-400">{f.venue}</td>
                  <td className={`px-3 py-2 text-right tabular-nums ${f.lag_ms > 500 ? "text-amber-400" : "text-slate-300"}`}>
                    <Flash value={Math.round(f.lag_ms)}>{Math.round(f.lag_ms)}ms</Flash>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">{f.msgs_s.toFixed(1)}</td>
                  <td className={`px-3 py-2 text-right tabular-nums ${f.staleness_s > 5 ? "text-amber-400" : "text-slate-300"}`}>{f.staleness_s.toFixed(1)}s</td>
                  <td className="px-3 py-2 text-center">
                    <span className={`inline-flex items-center gap-1.5 ${tone.status(f.status)}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${tone.dot(f.status)}`} />
                      {f.status}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <div className="h-1.5 bg-slate-800"><div className={`h-1.5 ${f.status === "ok" ? "bg-emerald-500/60" : "bg-amber-500/60"}`} style={{ width: `${barW}%` }} /></div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

// ---------- System Health ----------
function SystemHealth() {
  const [svcs, setSvcs] = React.useState(INFRA_SERVICES);
  React.useEffect(() => {
    const id = setInterval(() => {
      setSvcs(prev => prev.map(s => ({
        ...s,
        cpu: Math.max(1, Math.min(95, s.cpu + (Math.random() - 0.5) * 6)),
        p99_ms: Math.max(1, s.p99_ms + (Math.random() - 0.5) * s.p99_ms * 0.1),
      })));
    }, 2000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-4">
      <DemoBanner note="System health usa datos de ejemplo. Métricas reales en Grafana/Prometheus, no en este dashboard." />
      <div className="grid grid-cols-5 gap-px bg-slate-800/60 border border-slate-800/80">
        <div className="bg-[#0d1015] p-4"><Stat label="Services up" value={<span className="text-emerald-400">{svcs.length}/{svcs.length}</span>} sub="all healthy" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Avg CPU" value={`${Math.round(svcs.reduce((s, x) => s + x.cpu, 0) / svcs.length)}%`} sub="across fleet" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Total mem" value={`${(svcs.reduce((s, x) => s + x.mem, 0) / 1024).toFixed(1)}GB`} sub="rss" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="DB connections" value="42/200" sub="postgres pool" /></div>
        <div className="bg-[#0d1015] p-4"><Stat label="Alerts" value={<span className="text-emerald-400">0</span>} sub="last 24h" /></div>
      </div>

      <Panel padded={false}>
        <div className="px-4 py-3 border-b border-slate-800"><SectionTitle>Services</SectionTitle></div>
        <table className="w-full font-mono text-[11px]">
          <thead>
            <tr className="border-b border-slate-800 text-slate-500 uppercase tracking-wider text-[10px]">
              <th className="text-left px-3 py-2">Service</th>
              <th className="text-left px-3 py-2">Role</th>
              <th className="px-3 py-2 text-left w-40">CPU</th>
              <th className="text-right px-3 py-2">Mem (MB)</th>
              <th className="text-right px-3 py-2">p99</th>
              <th className="text-right px-3 py-2">Uptime</th>
              <th className="px-3 py-2 text-center">Status</th>
            </tr>
          </thead>
          <tbody>
            {svcs.map(s => (
              <tr key={s.name} className="border-b border-slate-800/60 hover:bg-slate-800/20">
                <td className="px-3 py-2 text-slate-200">{s.name}</td>
                <td className="px-3 py-2 text-slate-400">{s.role}</td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 bg-slate-800 w-20"><div className={`h-1.5 ${s.cpu > 70 ? "bg-amber-500/70" : "bg-emerald-500/60"}`} style={{ width: `${s.cpu}%` }} /></div>
                    <span className="text-slate-300 tabular-nums text-[10px]"><Flash value={Math.round(s.cpu)}>{Math.round(s.cpu)}%</Flash></span>
                  </div>
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{s.mem.toLocaleString()}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{Math.round(s.p99_ms)}ms</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{s.uptime_h > 24 ? `${Math.floor(s.uptime_h / 24)}d ${s.uptime_h % 24}h` : `${s.uptime_h}h`}</td>
                <td className="px-3 py-2 text-center">
                  <span className="inline-flex items-center gap-1.5 text-emerald-400">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                    {s.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <div className="grid grid-cols-2 gap-4">
        <Panel>
          <SectionTitle sub="HTTP 5xx last 24h">Error rate</SectionTitle>
          <div className="h-24 flex items-end gap-0.5">
            {Array.from({ length: 48 }).map((_, i) => {
              const h = Math.random() * 4 + 1;
              return <div key={i} className="flex-1 bg-emerald-500/30" style={{ height: `${h}%` }} />;
            })}
          </div>
          <div className="font-mono text-[10px] text-slate-500 mt-2 flex justify-between">
            <span>-24h</span><span>now</span>
          </div>
        </Panel>

        <Panel>
          <SectionTitle>Recent events</SectionTitle>
          <div className="space-y-1.5 font-mono text-[11px]">
            {[
              { t: "14:22:08", level: "info", msg: "engine.worker.pm · rebalanced 6 strategies" },
              { t: "13:44:21", level: "info", msg: "postgres.primary · checkpoint 2.4GB" },
              { t: "12:08:14", level: "warn", msg: "chainlink.btc-usd · staleness 42s (expected)" },
              { t: "09:12:00", level: "info", msg: "research.runner · completed backtest 7c441fe7" },
              { t: "08:00:00", level: "info", msg: "engine · daily reset · book=$10,000" },
            ].map((e, i) => (
              <div key={i} className="grid grid-cols-[62px_40px_1fr] gap-2">
                <span className="text-slate-500">{e.t}</span>
                <span className={`uppercase text-[9px] ${e.level === "warn" ? "text-amber-400" : "text-slate-500"}`}>{e.level}</span>
                <span className="text-slate-300 truncate">{e.msg}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------- Contest A/B ----------
function Contest() {
  const c = CONTEST;
  const confA = c.confusion_A, confB = c.confusion_B;

  const totA = confA.tp + confA.fn + confA.fp + confA.tn;
  const totB = confB.tp + confB.fn + confB.fp + confB.tn;

  const maxDay = 0.7;
  const minDay = 0.55;

  return (
    <div className="space-y-4">
      <DemoBanner note="Contest A/B usa datos de ejemplo. No hay sistema de A/B en producción todavía." />
      <Panel>
        <div className="flex items-start justify-between">
          <div>
            <h2 className="font-mono text-[14px] text-slate-100">{c.name}</h2>
            <p className="text-[11px] text-slate-500 font-mono mt-1">Started {c.started} · rolling {c.window} · evaluating direction predictions 5min ahead</p>
          </div>
          <div className="flex items-center gap-2">
            <Pill tone="emerald" size="xs"><Pulse size={4} /> live</Pill>
            <Pill tone="violet" size="xs">challenger leads by 2.9pp</Pill>
          </div>
        </div>
      </Panel>

      {/* Head-to-head */}
      <div className="grid grid-cols-2 gap-4">
        {["A", "B"].map(k => {
          const m = c.models[k];
          const isWinner = k === "B";
          return (
            <Panel key={k} className={isWinner ? "border-emerald-500/40" : ""}>
              <div className="flex items-center justify-between pb-3 border-b border-slate-800/60">
                <div className="flex items-center gap-2">
                  <span className={`font-mono text-[24px] ${isWinner ? "text-emerald-400" : "text-slate-400"}`}>{k}</span>
                  <div>
                    <div className="font-mono text-[12px] text-slate-200">{m.name}</div>
                    <div className="text-[10px] text-slate-500 font-mono">{fmt.num(m.n)} predictions</div>
                  </div>
                </div>
                {isWinner && <Pill tone="emerald" size="xs">LEADING</Pill>}
              </div>
              <div className="grid grid-cols-4 gap-3 pt-3">
                <div className="min-w-0"><div className="text-[9px] font-mono text-slate-500 uppercase">Accuracy</div><div className={`font-mono text-[18px] tabular-nums truncate ${isWinner ? "text-emerald-400" : "text-slate-200"}`}>{fmt.pct(m.accuracy, 2)}</div></div>
                <div className="min-w-0"><div className="text-[9px] font-mono text-slate-500 uppercase">Coverage</div><div className="font-mono text-[18px] tabular-nums truncate text-slate-200">{fmt.pct(m.coverage, 1)}</div></div>
                <div className="min-w-0"><div className="text-[9px] font-mono text-slate-500 uppercase">Brier</div><div className="font-mono text-[18px] tabular-nums truncate text-slate-200">{m.brier.toFixed(4)}</div></div>
                <div className="min-w-0"><div className="text-[9px] font-mono text-slate-500 uppercase">Log loss</div><div className="font-mono text-[18px] tabular-nums truncate text-slate-200">{m.logloss.toFixed(3)}</div></div>
              </div>
            </Panel>
          );
        })}
      </div>

      {/* Daily accuracy trend */}
      <Panel>
        <SectionTitle sub="Daily accuracy · A (champion) vs B (challenger)">Accuracy trend</SectionTitle>
        <div className="space-y-2">
          {c.daily.map(d => {
            const pctRange = maxDay - minDay;
            const aPct = (d.A - minDay) / pctRange;
            const bPct = (d.B - minDay) / pctRange;
            return (
              <div key={d.day} className="grid grid-cols-[44px_1fr_60px] items-center gap-3 font-mono text-[11px]">
                <span className="text-slate-500">{d.day}</span>
                <div className="relative h-5 bg-slate-800/40">
                  <div className="absolute top-0 bottom-0 border-r border-slate-700/60" style={{ left: `${((0.6 - minDay) / pctRange) * 100}%` }} />
                  <div className="absolute left-0 top-0.5 h-1.5 bg-sky-500/60" style={{ width: `${aPct * 100}%` }} title={`A: ${fmt.pct(d.A, 1)}`} />
                  <div className="absolute left-0 bottom-0.5 h-1.5 bg-emerald-500/60" style={{ width: `${bPct * 100}%` }} title={`B: ${fmt.pct(d.B, 1)}`} />
                </div>
                <span className="text-right text-slate-400"><span className="text-sky-400">{fmt.pct(d.A, 1)}</span> / <span className="text-emerald-400">{fmt.pct(d.B, 1)}</span></span>
              </div>
            );
          })}
        </div>
        <div className="mt-3 pt-3 border-t border-slate-800/60 flex gap-4 text-[10px] font-mono">
          <span className="flex items-center gap-1.5 text-slate-400"><span className="w-2 h-1.5 bg-sky-500/60" /> Model A</span>
          <span className="flex items-center gap-1.5 text-slate-400"><span className="w-2 h-1.5 bg-emerald-500/60" /> Model B</span>
          <span className="text-slate-500 ml-auto">0.60 accuracy threshold</span>
        </div>
      </Panel>

      {/* Confusion matrices */}
      <div className="grid grid-cols-2 gap-4">
        {[
          { k: "A", label: c.models.A.name, conf: confA, tot: totA, color: "sky" },
          { k: "B", label: c.models.B.name, conf: confB, tot: totB, color: "emerald" },
        ].map(({ k, label, conf, tot, color }) => {
          const cell = (v) => {
            const pct = v / tot;
            return (
              <div className={`relative flex flex-col items-center justify-center h-24 bg-${color}-500/10 border border-${color}-500/20`}
                style={{ backgroundColor: `rgba(${color === "sky" ? "56,189,248" : "52,211,153"}, ${0.04 + pct * 0.4})` }}>
                <div className="font-mono text-[20px] text-slate-100 tabular-nums">{v}</div>
                <div className="font-mono text-[10px] text-slate-400">{fmt.pct(pct, 1)}</div>
              </div>
            );
          };
          return (
            <Panel key={k}>
              <SectionTitle sub={label}>Confusion matrix · Model {k}</SectionTitle>
              <div className="grid grid-cols-[40px_1fr_1fr] gap-2">
                <div></div>
                <div className="text-center text-[9px] font-mono text-slate-500 uppercase pb-1">pred UP</div>
                <div className="text-center text-[9px] font-mono text-slate-500 uppercase pb-1">pred DOWN</div>

                <div className="flex items-center justify-end text-[9px] font-mono text-slate-500 uppercase">act UP</div>
                {cell(conf.tp)}
                {cell(conf.fn)}

                <div className="flex items-center justify-end text-[9px] font-mono text-slate-500 uppercase">act DOWN</div>
                {cell(conf.fp)}
                {cell(conf.tn)}
              </div>
              <div className="grid grid-cols-3 gap-2 mt-3 pt-3 border-t border-slate-800/60 font-mono text-[11px]">
                <div><div className="text-[9px] text-slate-500 uppercase">Precision</div><div className="text-slate-200">{fmt.pct(conf.tp / (conf.tp + conf.fp), 1)}</div></div>
                <div><div className="text-[9px] text-slate-500 uppercase">Recall</div><div className="text-slate-200">{fmt.pct(conf.tp / (conf.tp + conf.fn), 1)}</div></div>
                <div><div className="text-[9px] text-slate-500 uppercase">F1</div><div className="text-slate-200">{(2 * conf.tp / (2 * conf.tp + conf.fp + conf.fn)).toFixed(3)}</div></div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}

Object.assign(window, { Copilot, DataFreshness, SystemHealth, Contest });
