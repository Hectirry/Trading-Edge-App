// Shared primitives for TEA dashboard

// ---------- Tiny utilities ----------
const fmt = {
  money: (v, digits = 2) => {
    const sign = v >= 0 ? "+" : "-";
    return `${sign}$${Math.abs(v).toFixed(digits)}`;
  },
  moneyPlain: (v) => `$${v.toFixed(2)}`,
  pct: (v, digits = 2) => `${(v * 100).toFixed(digits)}%`,
  num: (v) => v.toLocaleString(),
  ms: (v) => `${v}ms`,
  sec: (v) => v < 60 ? `${v}s` : v < 3600 ? `${Math.floor(v / 60)}m${v % 60}s` : `${Math.floor(v / 3600)}h`,
  time: (d) => d.toTimeString().slice(0, 8),
  dur: (seconds) => {
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
  },
};

// ---------- Tone helpers ----------
const tone = {
  pnl: (v) => v >= 0 ? "text-emerald-400" : "text-rose-400",
  pnlBg: (v) => v >= 0 ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25" : "bg-rose-500/10 text-rose-400 border-rose-500/25",
  side: (s) => s === "BUY" ? "text-emerald-400" : "text-rose-400",
  status: (s) => {
    if (s === "ok" || s === "running" || s === "aligned") return "text-emerald-400";
    if (s === "slow" || s === "watch" || s === "degraded") return "text-amber-400";
    if (s === "error" || s === "paused" || s === "down") return "text-rose-400";
    return "text-slate-400";
  },
  dot: (s) => {
    if (s === "ok" || s === "running" || s === "aligned") return "bg-emerald-400";
    if (s === "slow" || s === "watch" || s === "degraded") return "bg-amber-400";
    if (s === "error" || s === "paused" || s === "down") return "bg-rose-400";
    return "bg-slate-400";
  },
};

// ---------- Ticker number (flashes on change) ----------
function Flash({ value, children, tone: forceTone }) {
  const [flash, setFlash] = React.useState(null);
  const prev = React.useRef(value);
  React.useEffect(() => {
    if (prev.current !== value) {
      const dir = typeof value === "number" && typeof prev.current === "number"
        ? (value > prev.current ? "up" : value < prev.current ? "down" : null)
        : "neutral";
      setFlash(forceTone || dir);
      prev.current = value;
      const t = setTimeout(() => setFlash(null), 600);
      return () => clearTimeout(t);
    }
  }, [value, forceTone]);

  const cls = flash === "up"   ? "bg-emerald-400/20 text-emerald-300"
            : flash === "down" ? "bg-rose-400/20 text-rose-300"
            : flash === "neutral" ? "bg-sky-400/15 text-sky-300"
            : "";
  return <span className={`transition-colors duration-500 ${cls}`}>{children}</span>;
}

// ---------- Tiny pulse dot ----------
function Pulse({ tone = "emerald", size = 6 }) {
  const color = tone === "emerald" ? "bg-emerald-400"
              : tone === "amber"   ? "bg-amber-400"
              : tone === "rose"    ? "bg-rose-400"
              : "bg-sky-400";
  return (
    <span className="relative inline-flex" style={{ width: size, height: size }}>
      <span className={`absolute inset-0 rounded-full ${color} opacity-70 animate-ping`} />
      <span className={`relative inline-flex rounded-full ${color}`} style={{ width: size, height: size }} />
    </span>
  );
}

// ---------- Sparkline ----------
function Sparkline({ points, width = 80, height = 22, stroke = "#34d399", fill = true, strokeWidth = 1.25 }) {
  if (!points || points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const coords = points.map((p, i) => [i * step, height - ((p - min) / range) * height]);
  const d = coords.map(([x, y], i) => (i === 0 ? `M${x.toFixed(1)},${y.toFixed(1)}` : `L${x.toFixed(1)},${y.toFixed(1)}`)).join(" ");
  const area = `${d} L${width},${height} L0,${height} Z`;
  const lastY = coords[coords.length - 1][1];
  return (
    <svg width={width} height={height} className="overflow-visible">
      {fill && <path d={area} fill={stroke} fillOpacity="0.12" />}
      <path d={d} fill="none" stroke={stroke} strokeWidth={strokeWidth} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={width} cy={lastY} r="1.6" fill={stroke} />
    </svg>
  );
}

// ---------- Bigger equity chart with grid ----------
function EquityChart({ points, width = 620, height = 140, stroke = "#34d399" }) {
  if (!points || points.length < 2) return null;
  const values = points.map(p => p.pnl ?? p);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const range = (max - min) || 1;
  const step = width / (values.length - 1);
  const y0 = height - ((0 - min) / range) * height;
  const coords = values.map((v, i) => [i * step, height - ((v - min) / range) * height]);
  const d = coords.map(([x, y], i) => (i === 0 ? `M${x.toFixed(1)},${y.toFixed(1)}` : `L${x.toFixed(1)},${y.toFixed(1)}`)).join(" ");
  const area = `${d} L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} className="w-full" preserveAspectRatio="none" viewBox={`0 0 ${width} ${height}`}>
      {/* grid */}
      {[0.25, 0.5, 0.75].map(g => (
        <line key={g} x1="0" x2={width} y1={height * g} y2={height * g} stroke="#1e293b" strokeDasharray="2 4" />
      ))}
      {/* zero line */}
      <line x1="0" x2={width} y1={y0} y2={y0} stroke="#334155" strokeDasharray="3 3" />
      <path d={area} fill={stroke} fillOpacity="0.12" />
      <path d={d} fill="none" stroke={stroke} strokeWidth="1.5" />
    </svg>
  );
}

// ---------- Section heading ----------
function SectionTitle({ children, right, sub }) {
  return (
    <div className="flex items-end justify-between border-b border-slate-800 pb-2 mb-3">
      <div>
        <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-slate-400">{children}</h3>
        {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
      </div>
      {right && <div className="text-[11px] text-slate-400">{right}</div>}
    </div>
  );
}

// ---------- Panel ----------
function Panel({ children, className = "", padded = true }) {
  return (
    <div className={`bg-[#0d1015] border border-slate-800/80 ${padded ? "p-4" : ""} ${className}`}>
      {children}
    </div>
  );
}

// ---------- KPI stat ----------
function Stat({ label, value, delta, tone: t, sub, sparkPoints, sparkStroke }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">{label}</div>
      <div className="flex items-baseline gap-2">
        <span className={`font-mono text-2xl tabular-nums ${t || "text-slate-100"}`}>{value}</span>
        {delta !== undefined && delta !== null && (
          <span className={`font-mono text-[11px] tabular-nums ${tone.pnl(delta)}`}>
            {delta >= 0 ? "▲" : "▼"} {Math.abs(delta).toFixed(2)}%
          </span>
        )}
      </div>
      {sub && <div className="text-[11px] text-slate-500 font-mono">{sub}</div>}
      {sparkPoints && <div className="mt-1"><Sparkline points={sparkPoints} stroke={sparkStroke || "#34d399"} width={100} height={18} /></div>}
    </div>
  );
}

// ---------- Pill / tag ----------
function Pill({ children, tone: t = "slate", size = "sm" }) {
  const styles = {
    slate:   "bg-slate-500/10 text-slate-300 border-slate-700/60",
    emerald: "bg-emerald-500/10 text-emerald-300 border-emerald-500/25",
    rose:    "bg-rose-500/10 text-rose-300 border-rose-500/25",
    amber:   "bg-amber-500/10 text-amber-300 border-amber-500/25",
    violet:  "bg-violet-500/10 text-violet-300 border-violet-500/25",
    sky:     "bg-sky-500/10 text-sky-300 border-sky-500/25",
  };
  const sz = size === "xs" ? "text-[10px] px-1.5 py-px" : "text-[10.5px] px-2 py-0.5";
  return <span className={`inline-flex items-center gap-1 font-mono uppercase tracking-wider border ${styles[t] || styles.slate} ${sz}`}>{children}</span>;
}

// ---------- Icons (inline SVG, 14px) ----------
const Icon = {
  overview:  (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>,
  strategy:  (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>,
  research:  (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><circle cx="11" cy="11" r="6"/><path d="M21 21l-4.3-4.3"/></svg>,
  llm:       (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M21 12a8 8 0 1 1-3.2-6.4L21 3v6h-6"/></svg>,
  feeds:     (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M3 11a14 14 0 0 1 18 0"/><path d="M7 15a8 8 0 0 1 10 0"/><circle cx="12" cy="19" r="1.2"/></svg>,
  health:    (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M3 12h4l2-5 4 10 2-5h6"/></svg>,
  contest:   (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M6 9h12"/><path d="M6 15h12"/><circle cx="6" cy="9" r="2"/><circle cx="18" cy="15" r="2"/></svg>,
  pause:     (p) => <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" {...p}><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>,
  play:      (p) => <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" {...p}><path d="M6 4l14 8-14 8V4z"/></svg>,
  kill:      (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><circle cx="12" cy="12" r="9"/><path d="M12 7v5"/><circle cx="12" cy="16" r="0.8" fill="currentColor"/></svg>,
  arrowUp:   (p) => <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor" {...p}><path d="M12 4l8 10h-5v6H9v-6H4z"/></svg>,
  arrowDown: (p) => <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor" {...p}><path d="M12 20l8-10h-5V4H9v6H4z"/></svg>,
  send:      (p) => <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>,
  filter:    (p) => <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M4 5h16l-6 8v6l-4-2v-4z"/></svg>,
  refresh:   (p) => <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>,
};

Object.assign(window, {
  fmt, tone, Flash, Pulse, Sparkline, EquityChart,
  SectionTitle, Panel, Stat, Pill, Icon,
});
