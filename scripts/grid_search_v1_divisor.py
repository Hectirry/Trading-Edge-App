"""Grid search ``momentum_divisor_bps`` for last_90s_forecaster_v1.

For each candidate divisor, replay resolved markets (from polybot-agent
sqlite), build the same t=210 feature context the live strategy sees,
run the v1 decision tree, and score hypothetical PnL against the
ground-truth label (``close_price > open_price``).

Usage::

    docker compose exec tea-engine python scripts/grid_search_v1_divisor.py \\
        --from 2025-11-01 --to 2026-04-25 \\
        --polybot-agent /btc-tendencia-data/polybot-agent.db \\
        --divisors 20 30 40 50 60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

# Reuse the training CLI's loaders + sample builder.
from trading.cli.train_last90s import (
    _load_ohlcv_5m,
    _load_resolved_markets,
    build_samples,
)

log = logging.getLogger("grid_search.v1_divisor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Feature indices — kept in sync with _v2_features.FEATURE_NAMES ordering.
# v1 only needs a subset so we fetch the exact fields we care about.
FEATURE_IDX = {
    "m90_bps": 2,
    "rv_90s": 3,
    "ema8_vs_ema34_pct": 5,
    "adx_14": 6,
    "consecutive_same_dir": 7,
    "regime_uptrend": 8,
    "regime_downtrend": 9,
    "regime_range": 10,
    "implied_prob_yes": 11,
    "pm_spread_bps": 15,
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _regime_from_onehot(onehot: tuple[float, float, float]) -> str:
    if onehot[0] >= 0.5:
        return "uptrend"
    if onehot[1] >= 0.5:
        return "downtrend"
    return "range"


def _evaluate(
    samples, divisor: float, edge_threshold: float = 0.04, spread_max: float = 150.0
) -> dict:
    n_entries = 0
    n_wins = 0
    pnl_usd = 0.0
    reasons: dict[str, int] = {}

    for s in samples:
        f = s.features
        m90 = f[FEATURE_IDX["m90_bps"]]
        regime = _regime_from_onehot(
            (
                f[FEATURE_IDX["regime_uptrend"]],
                f[FEATURE_IDX["regime_downtrend"]],
                f[FEATURE_IDX["regime_range"]],
            )
        )
        implied = f[FEATURE_IDX["implied_prob_yes"]]
        spread = f[FEATURE_IDX["pm_spread_bps"]]

        micro_prob = 0.5 + _clamp(m90 / divisor, -0.45, 0.45)
        edge = micro_prob - implied

        if spread > spread_max:
            reasons["spread_too_wide"] = reasons.get("spread_too_wide", 0) + 1
            continue
        if regime == "uptrend" and micro_prob <= 0.5:
            reasons["macro_contradicts_micro"] = reasons.get("macro_contradicts_micro", 0) + 1
            continue
        if regime == "downtrend" and micro_prob >= 0.5:
            reasons["macro_contradicts_micro"] = reasons.get("macro_contradicts_micro", 0) + 1
            continue
        if abs(edge) < edge_threshold:
            reasons["edge_below_threshold"] = reasons.get("edge_below_threshold", 0) + 1
            continue

        # Side is YES_UP if edge positive else YES_DOWN.
        side_up = edge > 0
        went_up = s.label == 1
        win = (side_up and went_up) or (not side_up and not went_up)

        # Entry price = implied; payout = $1 if win else $0; stake = $5;
        # shares = stake / implied.
        stake = 5.0
        entry = max(0.05, min(0.95, implied))  # clamp to sane range
        shares = stake / entry
        pnl = shares * (1.0 if win else 0.0) - stake - 0.25  # fee approx
        pnl_usd += pnl
        n_entries += 1
        n_wins += 1 if win else 0

    return {
        "divisor": divisor,
        "n_samples_considered": len(samples),
        "n_entries": n_entries,
        "n_wins": n_wins,
        "win_rate": (n_wins / n_entries) if n_entries else 0.0,
        "pnl_usd": round(pnl_usd, 4),
        "pnl_per_entry": round(pnl_usd / n_entries, 4) if n_entries else 0.0,
        "top_skip_reasons": sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:3],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--polybot-agent", default="/btc-tendencia-data/polybot-agent.db")
    ap.add_argument("--polybot-btc5m", default="/polybot-btc5m-data/polybot_agent.db")
    ap.add_argument("--divisors", type=float, nargs="+", default=[20.0, 30.0, 40.0, 50.0, 60.0])
    args = ap.parse_args()

    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="tea_grid_"))

    def _snapshot(src: str) -> str:
        p = Path(src)
        if not p.exists():
            return src
        dst = tmp_dir / p.name
        shutil.copy2(p, dst)
        return str(dst)

    agent_snap = _snapshot(args.polybot_agent)
    btc5m_snap = _snapshot(args.polybot_btc5m)

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)

    pg_dsn = (
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}"
    )

    ma = _load_resolved_markets(Path(btc5m_snap), slug_encodes_open_ts=False, pg_dsn=pg_dsn)
    for m in ma:
        m["_source"] = btc5m_snap
    mb = _load_resolved_markets(Path(agent_snap), slug_encodes_open_ts=True, pg_dsn=pg_dsn)
    for m in mb:
        m["_source"] = agent_snap
    markets = [
        m for m in (ma + mb) if t_from.timestamp() <= float(m["close_ts"]) <= t_to.timestamp()
    ]
    log.info("resolved markets: %d", len(markets))
    candles = _load_ohlcv_5m(
        pg_dsn,
        int(t_from.timestamp()) - 3600,
        int(t_to.timestamp()) + 3600,
    )
    log.info("ohlcv 5m rows: %d", len(candles))

    samples = build_samples(
        markets,
        sqlite_sources=[Path(btc5m_snap), Path(agent_snap)],
        candles_5m=candles,
    )
    log.info("feature samples: %d / markets=%d", len(samples), len(markets))

    # Inject a realistic implied_prob proxy: polybot-agent doesn't store
    # Polymarket book snapshots at t=210, so samples carry implied=0.5.
    # For the grid search we want a directional signal; shift implied
    # slightly off 0.5 using the sign of m90 only when |m90| > 5 bps so
    # the edge filter still can differentiate divisors.
    for s in samples:
        m90 = s.features[FEATURE_IDX["m90_bps"]]
        if abs(m90) > 5.0:
            s.features[FEATURE_IDX["implied_prob_yes"]] = 0.5 + (
                -0.02 if m90 > 0 else 0.02
            )  # simulate a slightly mis-priced book

    results = [_evaluate(samples, d) for d in args.divisors]
    best = max(results, key=lambda r: r["pnl_per_entry"])

    print(json.dumps({"results": results, "best_divisor": best["divisor"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
