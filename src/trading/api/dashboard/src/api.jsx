// Live API client — talks to /api/v1/* on same origin. The cookie
// `tea_token` set by /login authenticates every request via fetch's
// default same-origin credentials.

const API = {
  async overview() {
    const r = await fetch("/api/v1/dashboard/overview", { credentials: "same-origin" });
    if (!r.ok) throw new Error(`overview: ${r.status}`);
    return r.json();
  },

  async pause(name) {
    const r = await fetch(`/api/v1/strategies/${encodeURIComponent(name)}/pause`, {
      method: "POST", credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`pause ${name}: ${r.status}`);
    return r.json();
  },

  async resume(name) {
    const r = await fetch(`/api/v1/strategies/${encodeURIComponent(name)}/resume`, {
      method: "POST", credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`resume ${name}: ${r.status}`);
    return r.json();
  },

  async armKill() {
    const r = await fetch("/api/v1/killswitch", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "sí lo entiendo" }),
    });
    if (!r.ok) throw new Error(`killswitch arm: ${r.status}`);
    return r.json();
  },

  async disarmKill() {
    const r = await fetch("/api/v1/killswitch_off", {
      method: "POST", credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`killswitch off: ${r.status}`);
    return r.json();
  },

  async chat({ session_id, message, model }) {
    const r = await fetch("/api/v1/llm/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id, message, model, context_refs: [] }),
    });
    if (!r.ok) {
      let detail = `chat: ${r.status}`;
      try { const j = await r.json(); if (j.detail) detail = j.detail; } catch (e) {}
      throw new Error(detail);
    }
    return r.json();
  },
};

// Hydrate window.STRATEGIES / LIVE_TRADES / BACKTESTS from the live
// view-model endpoint. Falls back to whatever mockData.jsx left on
// window if the endpoint fails (so the dashboard still renders during
// auth/network blips).
async function bootstrapOverview() {
  try {
    const o = await API.overview();
    // The endpoint is the source of truth — even an empty `strategies`
    // array means "no enabled strategies right now", not "fall back to
    // mocks". Mocks are only used if the request itself fails.
    window.STRATEGIES = o.strategies || [];
    window.LIVE_TRADES = o.recent_trades || [];
    window.BACKTESTS = o.backtests || [];
    window.__TEA_ENGINE = o.engine;
    window.__TEA_TOTALS = o.totals;
    return o;
  } catch (e) {
    console.warn("dashboard overview load failed, using mock fallback:", e);
    window.__TEA_BOOTSTRAP_ERROR = String(e);
    return null;
  }
}

window.API = API;
window.bootstrapOverview = bootstrapOverview;

// Banner used by tabs whose data does not have a server endpoint yet —
// keeps the visual demo intact while making it explicit that the
// numbers are not from the live system.
function DemoBanner({ note }) {
  return (
    <div className="flex items-center gap-2 px-3 py-2 mb-3 border border-amber-500/30 bg-amber-500/5 font-mono text-[10.5px] text-amber-300">
      <span className="px-1.5 py-px border border-amber-500/40 text-[9px] uppercase tracking-wider">demo</span>
      <span className="text-amber-300/90">{note || "Datos de ejemplo — no hay endpoint live para esta vista todavía."}</span>
    </div>
  );
}
window.DemoBanner = DemoBanner;
