// Mock data for TEA dashboard
// 6 strategies: 3 from backtests + 3 invented plausible ones

const STRATEGIES = [
  {
    id: "imbalance_v3",
    name: "imbalance_v3",
    label: "Imbalance v3",
    venue: "Polymarket",
    asset: "BTC-5min",
    status: "running",
    pnl_24h: 42.31,
    pnl_7d: 187.42,
    win_rate: 0.672,
    n_trades_24h: 34,
    sharpe: 0.42,
    mdd: -12.4,
    last_signal_s: 8,
    heartbeat_ms: 312,
    horizon_s: 900,
  },
  {
    id: "oracle_lag_sniper_v1",
    name: "oracle_lag_sniper_v1",
    label: "Oracle Lag Sniper v1",
    venue: "Polymarket",
    asset: "BTC-5min",
    status: "running",
    pnl_24h: 18.06,
    pnl_7d: 62.18,
    win_rate: 0.488,
    n_trades_24h: 19,
    sharpe: 0.21,
    mdd: -8.1,
    last_signal_s: 22,
    heartbeat_ms: 94,
    horizon_s: 60,
  },
  {
    id: "trend_confirm_t1_v1",
    name: "trend_confirm_t1_v1",
    label: "Trend Confirm T1 v1",
    venue: "Polymarket",
    asset: "BTC-5min",
    status: "running",
    pnl_24h: 27.94,
    pnl_7d: 145.02,
    win_rate: 0.551,
    n_trades_24h: 42,
    sharpe: 0.38,
    mdd: -14.6,
    last_signal_s: 3,
    heartbeat_ms: 208,
    horizon_s: 180,
  },
  {
    id: "vwap_revert_btc_v2",
    name: "vwap_revert_btc_v2",
    label: "VWAP Revert BTC v2",
    venue: "Binance",
    asset: "BTCUSDT",
    status: "running",
    pnl_24h: 61.48,
    pnl_7d: 312.87,
    win_rate: 0.594,
    n_trades_24h: 28,
    sharpe: 0.71,
    mdd: -19.2,
    last_signal_s: 14,
    heartbeat_ms: 42,
    horizon_s: 300,
  },
  {
    id: "funding_skew_v1",
    name: "funding_skew_v1",
    label: "Funding Skew v1",
    venue: "Bybit",
    asset: "ETHUSDT",
    status: "running",
    pnl_24h: 12.77,
    pnl_7d: 89.44,
    win_rate: 0.612,
    n_trades_24h: 11,
    sharpe: 0.55,
    mdd: -6.8,
    last_signal_s: 47,
    heartbeat_ms: 68,
    horizon_s: 3600,
  },
  {
    id: "orderbook_pressure_v2",
    name: "orderbook_pressure_v2",
    label: "Orderbook Pressure v2",
    venue: "Binance",
    asset: "SOLUSDT",
    status: "running",
    pnl_24h: 9.15,
    pnl_7d: 48.22,
    win_rate: 0.524,
    n_trades_24h: 52,
    sharpe: 0.29,
    mdd: -11.3,
    last_signal_s: 2,
    heartbeat_ms: 56,
    horizon_s: 120,
  },
];

// Live trades ticker
const LIVE_TRADES = [
  { t: "14:32:18", strat: "trend_confirm_t1_v1", side: "BUY",  venue: "Polymarket", sym: "BTC-5m", px: "0.5412", qty: 120, pnl: 1.84 },
  { t: "14:32:11", strat: "vwap_revert_btc_v2",   side: "SELL", venue: "Binance",    sym: "BTCUSDT", px: "94812.40", qty: 0.012, pnl: 3.12 },
  { t: "14:32:04", strat: "orderbook_pressure_v2",side: "BUY",  venue: "Binance",    sym: "SOLUSDT", px: "184.22", qty: 2.8, pnl: -0.42 },
  { t: "14:31:57", strat: "imbalance_v3",         side: "BUY",  venue: "Polymarket", sym: "BTC-5m", px: "0.4871", qty: 200, pnl: 2.60 },
  { t: "14:31:48", strat: "funding_skew_v1",      side: "SELL", venue: "Bybit",      sym: "ETHUSDT", px: "3284.12", qty: 0.08, pnl: 0.94 },
  { t: "14:31:42", strat: "oracle_lag_sniper_v1", side: "BUY",  venue: "Polymarket", sym: "BTC-5m", px: "0.6204", qty: 80,  pnl: -0.32 },
  { t: "14:31:35", strat: "trend_confirm_t1_v1",  side: "SELL", venue: "Polymarket", sym: "BTC-5m", px: "0.5488", qty: 150, pnl: 1.12 },
  { t: "14:31:28", strat: "vwap_revert_btc_v2",   side: "BUY",  venue: "Binance",    sym: "BTCUSDT", px: "94798.80", qty: 0.010, pnl: 2.48 },
  { t: "14:31:19", strat: "imbalance_v3",         side: "SELL", venue: "Polymarket", sym: "BTC-5m", px: "0.4902", qty: 180, pnl: 0.72 },
  { t: "14:31:12", strat: "orderbook_pressure_v2",side: "SELL", venue: "Binance",    sym: "SOLUSDT", px: "184.08", qty: 3.1, pnl: 1.08 },
  { t: "14:31:03", strat: "funding_skew_v1",      side: "BUY",  venue: "Bybit",      sym: "ETHUSDT", px: "3281.44", qty: 0.12, pnl: -0.18 },
  { t: "14:30:54", strat: "trend_confirm_t1_v1",  side: "BUY",  venue: "Polymarket", sym: "BTC-5m", px: "0.5401", qty: 140, pnl: 0.96 },
];

const DATA_FEEDS = [
  { name: "polymarket.orderbook.btc-5m", venue: "Polymarket", lag_ms: 38,  msgs_s: 412, staleness_s: 0.4, status: "ok"   },
  { name: "polymarket.trades.btc-5m",    venue: "Polymarket", lag_ms: 22,  msgs_s: 184, staleness_s: 0.2, status: "ok"   },
  { name: "polymarket.oracle.feed",      venue: "Polymarket", lag_ms: 112, msgs_s: 8,   staleness_s: 1.1, status: "ok"   },
  { name: "binance.spot.btcusdt",        venue: "Binance",    lag_ms: 18,  msgs_s: 2480,staleness_s: 0.1, status: "ok"   },
  { name: "binance.spot.solusdt",        venue: "Binance",    lag_ms: 21,  msgs_s: 1842,staleness_s: 0.1, status: "ok"   },
  { name: "binance.futures.btcusdt",     venue: "Binance",    lag_ms: 24,  msgs_s: 3120,staleness_s: 0.1, status: "ok"   },
  { name: "bybit.spot.ethusdt",          venue: "Bybit",      lag_ms: 32,  msgs_s: 1204,staleness_s: 0.2, status: "ok"   },
  { name: "bybit.futures.ethusdt",       venue: "Bybit",      lag_ms: 29,  msgs_s: 2042,staleness_s: 0.1, status: "ok"   },
  { name: "coinbase.reference.btc",      venue: "Coinbase",   lag_ms: 48,  msgs_s: 820, staleness_s: 0.3, status: "ok"   },
  { name: "chainlink.btc-usd.aggregator",venue: "Chainlink",  lag_ms: 1840,msgs_s: 0.1, staleness_s: 42.1,status: "slow" },
];

const INFRA_SERVICES = [
  { name: "fastapi.gateway",       role: "API",       status: "ok",   cpu: 18, mem: 342, p99_ms: 24,  uptime_h: 412 },
  { name: "engine.worker.pm",      role: "Engine",    status: "ok",   cpu: 44, mem: 712, p99_ms: 8,   uptime_h: 412 },
  { name: "engine.worker.cex",     role: "Engine",    status: "ok",   cpu: 38, mem: 684, p99_ms: 6,   uptime_h: 412 },
  { name: "research.runner",       role: "Research",  status: "ok",   cpu: 12, mem: 248, p99_ms: 48,  uptime_h: 88  },
  { name: "postgres.primary",      role: "Database",  status: "ok",   cpu: 22, mem: 4820,p99_ms: 3,   uptime_h: 892 },
  { name: "postgres.replica",      role: "Database",  status: "ok",   cpu: 14, mem: 4720,p99_ms: 4,   uptime_h: 892 },
  { name: "redis.cache",           role: "Cache",     status: "ok",   cpu: 8,  mem: 512, p99_ms: 1,   uptime_h: 892 },
  { name: "grafana",               role: "Observab.", status: "ok",   cpu: 6,  mem: 184, p99_ms: 82,  uptime_h: 412 },
  { name: "prometheus",            role: "Observab.", status: "ok",   cpu: 14, mem: 812, p99_ms: 18,  uptime_h: 412 },
  { name: "llm.proxy",             role: "Copilot",   status: "ok",   cpu: 4,  mem: 124, p99_ms: 412, uptime_h: 412 },
];

const LLM_MESSAGES = [
  { role: "user",      t: "14:28:44", content: "Why did imbalance_v3 underperform on 2026-04-18?" },
  { role: "assistant", t: "14:28:47", content: "On 2026-04-18 imbalance_v3 ran 513 trades with a 71.9% win rate but still lost $73.10 — the classic 'many small wins, few large losses' pattern. Avg loss (-$1.43) was ~4× avg win ($0.37). The backtest shows profit factor 0.65 and fees consumed $88.68, larger than the gross loss from trades alone. Fees + skewed R:R are the culprits.", cost: 0.0042, tokens: 428, model: "claude-haiku-4-5" },
  { role: "user",      t: "14:29:12", content: "Suggest a fix." },
  { role: "assistant", t: "14:29:16", content: "Three options, ranked by expected impact:\n\n1. **Raise stop-loss threshold** — current fills show avg loss 4× avg win. Tighten SL to ~1.5× avg win target.\n2. **Reduce trade frequency** — 57 trades/day at this horizon eats fees. Require stronger imbalance confirmation (z-score ≥ 2.2 instead of 1.8).\n3. **Regime filter** — perf degrades in low-vol. Skip entries when 1h realized vol < 0.6%.\n\nI can queue a walk-forward backtest for option 2 — say the word.", cost: 0.0088, tokens: 892, model: "claude-haiku-4-5" },
];

const LLM_COSTS_7D = [
  { day: "04-17", cost: 2.42, tokens: 284_000, calls: 142 },
  { day: "04-18", cost: 3.18, tokens: 372_000, calls: 198 },
  { day: "04-19", cost: 2.87, tokens: 324_000, calls: 174 },
  { day: "04-20", cost: 4.12, tokens: 488_000, calls: 226 },
  { day: "04-21", cost: 3.44, tokens: 398_000, calls: 184 },
  { day: "04-22", cost: 2.92, tokens: 332_000, calls: 162 },
  { day: "04-23", cost: 1.88, tokens: 218_000, calls: 108 },
];

// Paper-vs-backtest: expected vs actual per strategy
const PAPER_VS_BT = [
  { strat: "imbalance_v3",          bt_sharpe: 0.51, paper_sharpe: 0.42, bt_winrate: 0.68, paper_winrate: 0.67, drift: "aligned" },
  { strat: "oracle_lag_sniper_v1",  bt_sharpe: 0.28, paper_sharpe: 0.21, bt_winrate: 0.52, paper_winrate: 0.49, drift: "aligned" },
  { strat: "trend_confirm_t1_v1",   bt_sharpe: 0.44, paper_sharpe: 0.38, bt_winrate: 0.58, paper_winrate: 0.55, drift: "aligned" },
  { strat: "vwap_revert_btc_v2",    bt_sharpe: 0.82, paper_sharpe: 0.71, bt_winrate: 0.62, paper_winrate: 0.59, drift: "aligned" },
  { strat: "funding_skew_v1",       bt_sharpe: 0.61, paper_sharpe: 0.55, bt_winrate: 0.64, paper_winrate: 0.61, drift: "aligned" },
  { strat: "orderbook_pressure_v2", bt_sharpe: 0.41, paper_sharpe: 0.29, bt_winrate: 0.56, paper_winrate: 0.52, drift: "watch" },
];

// Walk-forward windows for one strategy
const WALK_FORWARD = {
  strategy: "imbalance_v3",
  windows: [
    { id: "W1",  train: "02-22 → 03-08", test: "03-08 → 03-15", is_sharpe: 0.88, oos_sharpe: 0.62, oos_pnl:  42.14 },
    { id: "W2",  train: "03-01 → 03-15", test: "03-15 → 03-22", is_sharpe: 0.72, oos_sharpe: 0.48, oos_pnl:  28.82 },
    { id: "W3",  train: "03-08 → 03-22", test: "03-22 → 03-29", is_sharpe: 0.91, oos_sharpe: 0.54, oos_pnl:  38.91 },
    { id: "W4",  train: "03-15 → 03-29", test: "03-29 → 04-05", is_sharpe: 0.64, oos_sharpe: -0.12, oos_pnl: -18.44 },
    { id: "W5",  train: "03-22 → 04-05", test: "04-05 → 04-12", is_sharpe: 0.81, oos_sharpe: 0.42, oos_pnl:  22.18 },
    { id: "W6",  train: "03-29 → 04-12", test: "04-12 → 04-19", is_sharpe: 0.77, oos_sharpe: 0.38, oos_pnl:  18.74 },
    { id: "W7",  train: "04-05 → 04-19", test: "04-19 → 04-23", is_sharpe: 0.68, oos_sharpe: 0.51, oos_pnl:  24.12 },
  ],
};

// Contest A/B — two signal models scored on prediction accuracy
const CONTEST = {
  id: "btc_5m_direction_contest",
  name: "BTC 5m Direction — Model A vs Model B",
  started: "2026-04-16",
  window: "7d rolling",
  models: {
    A: { name: "ensemble_v3 (champion)", accuracy: 0.6124, coverage: 0.81, n: 4812, brier: 0.2284, logloss: 0.612 },
    B: { name: "ensemble_v4 (challenger)", accuracy: 0.6412, coverage: 0.73, n: 4124, brier: 0.2108, logloss: 0.572 },
  },
  // Confusion matrices: rows = actual (up/down), cols = predicted (up/down)
  confusion_A: { tp: 1482, fn: 924,  fp: 942, tn: 1464 },
  confusion_B: { tp: 1412, fn: 712,  fp: 768, tn: 1232 },
  // Daily accuracy series
  daily: [
    { day: "04-17", A: 0.591, B: 0.622 },
    { day: "04-18", A: 0.604, B: 0.648 },
    { day: "04-19", A: 0.612, B: 0.638 },
    { day: "04-20", A: 0.618, B: 0.652 },
    { day: "04-21", A: 0.608, B: 0.641 },
    { day: "04-22", A: 0.614, B: 0.644 },
    { day: "04-23", A: 0.619, B: 0.637 },
  ],
};

// Equity curve for overview — aggregate PnL last 24h
const EQUITY_24H = Array.from({ length: 96 }, (_, i) => {
  const base = i * 1.8;
  const noise = Math.sin(i * 0.3) * 12 + Math.sin(i * 0.12) * 18 + (Math.random() - 0.5) * 4;
  return { t: i, pnl: base + noise + 30 };
});

Object.assign(window, {
  STRATEGIES,
  LIVE_TRADES,
  DATA_FEEDS,
  INFRA_SERVICES,
  LLM_MESSAGES,
  LLM_COSTS_7D,
  PAPER_VS_BT,
  WALK_FORWARD,
  CONTEST,
  EQUITY_24H,
});
