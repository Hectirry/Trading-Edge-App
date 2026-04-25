---
name: tea-forensics
description: >
  Audit protocol for stored backtest/training labels in Trading-Edge-App.
  Re-derives ground truth canonically from market_data.crypto_ohlcv 1m
  Binance close, compares against stored values, stratifies disagreement,
  and prints blast radius. Pattern reused from the 2026-04-25 forensic on
  trend_confirm_t1_v1 (9.7 % win rate FAIL → off-by-one strike + frozen
  chainlink) and the polybot SQLite ground-truth audit (40.5 % inverted
  v2 labels). Invoke when investigating "why did this strategy score so
  badly", "audit polybot/paper labels", or "verify ground truth".
---

Two audit shapes, one truth source. Both reuse `crypto_ohlcv` 1m
Binance BTCUSDT close as canonical settle.

## Audit shape A — backtest trade resolution

Entry point: a `research.backtests.id` whose stored win-rate looks
wrong. Reproduce per-trade resolution and compare to canonical.

```sql
-- 1. Load the trades.
SELECT trade_idx, instrument AS slug, strategy_side,
       EXTRACT(EPOCH FROM entry_ts) AS entry_ts,
       entry_price, exit_price, pnl, t_in_window_s, edge_bps, metadata
FROM research.backtest_trades
WHERE backtest_id = $1::uuid
ORDER BY entry_ts;
```

For each trade, compute three settles:

```sql
-- (a) what the loader thinks the OPEN price is (5m close at window_close-300)
SELECT close FROM market_data.crypto_ohlcv
WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m'
  AND ts = to_timestamp($1);   -- window_close - 300

-- (b) canonical OPEN (1m close at window_open)
SELECT close FROM market_data.crypto_ohlcv
WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m'
  AND ts = to_timestamp($1);   -- window_close - 300

-- (c) canonical SETTLE (1m close at window_close)
SELECT close FROM market_data.crypto_ohlcv
WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m'
  AND ts = to_timestamp($1);   -- window_close
```

Stored `won = (final_price > open_price) == (side == 'YES_UP')`.
Canonical `won = (canonical_settle > canonical_open) == (side == 'YES_UP')`.
Stratify disagreement by:

- |Δ open_price| in bps (`(loader_open - canonical_open) / canonical_open * 1e4`)
- whether `final_price` came from chainlink (truthy) vs spot fallback
- `t_in_window_s` of the entry tick

If > 5 % of trades disagree, escalate: load the loader's `open_price`
construction and the driver's `final_price` construction side-by-side
with the canonical 1m series.

Reference implementation: `scripts/forensics_trend_fail.py`. The 31-trade
forensic that flagged the `interval='5m'` bug + chainlink-frozen-final
pair lives in `forensics_trend_fail.py:68-178`.

## Audit shape B — training-label inversion

Entry point: an LightGBM model with suspicious AUC (≪ 0.5 or
suspiciously consistent across folds). Audit polybot SQLite or any
SQLite that feeds `train_last90s._load_resolved_markets`.

```python
# 1. Mirror _load_resolved_markets exactly. Slug timestamp encoding
#    differs per source — polybot-btc5m → close_ts, BTC-Tendencia → open_ts.
trades = con.execute(
    """SELECT DISTINCT market_slug FROM trades
       WHERE resolution IN ('win', 'loss')
         AND market_slug LIKE 'btc-%updown-5m-%'"""
).fetchall()
```

For each market, query the *first* tick:

```sql
SELECT open_price, spot_price, chainlink_price
FROM ticks
WHERE market_slug = ?
ORDER BY ts ASC LIMIT 1
```

Three checks (all pure read):

1. **`open_price` vs first chainlink**: if equal in > 50 % of markets,
   the loader is using chainlink as open (= biased; chainlink lags spot).
2. **Distinct chainlink values per window**: if the median is 1 (only
   one chainlink value over the whole 5 min), the chainlink feed is
   frozen. Settle from chainlink → systematic bias.
3. **Stored vs canonical labels**: re-compute label as
   `(binance_1m_close[close_minute] > binance_1m_close[open_minute])`
   and compare to `(close_price > open_price)` from SQLite.

Quantify blast radius: `inverted_count / training_set_size`. > 30 % is
catastrophic; the model must be retrained from re-derived labels and
the existing trained model must be flagged in `metrics.ground_truth_audit`
(or simply de-promoted with `is_active=false`).

Reference implementation: `scripts/audit_polybot_groundtruth.py`. Stratify
disagreement by distinct-CL bucket (`{1, 2-3, 4+}`) and by |Δ| in bps.

## Patterns to keep

- **Read-only**. No INSERT, UPDATE, DELETE in audit scripts. Print or
  CSV.
- **Reuse the loader logic exactly**, including the `slug_encodes_open_ts`
  flag — bugs hide in the divergence between audit code and prod loader.
- **Stratify before concluding**. Aggregate inversion rate masks the
  pattern; bucket by data-source feature (chainlink frozen yes/no, |Δ|
  size) to see *where* the inversions live.
- **Document the audit in `estrategias/`**. Move-to-descartadas pattern:
  audit becomes its own `.md` file with motive + fix scope.

## Don't

- Don't conclude from a small subset. Audit at least N=100 markets or
  the full training set.
- Don't fix in the audit script. Audit produces a report; the fix is a
  separate commit (e.g. `_load_resolved_markets` change), with its own
  tests, after the user confirms scope.
- Don't trust `chainlink_price` as ground truth. Polygon EAC chainlink
  is a known-frozen oracle in our market window; canonical truth lives
  in `market_data.crypto_ohlcv` (Binance 1m).
