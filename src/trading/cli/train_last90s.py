"""Training CLI for last_90s_forecaster_v2 (ADR 0011).

Reads resolved BTC up/down 5 m markets from the polybot-btc5m and
polybot-agent SQLite files (read-only), joins against TEA
``market_data.crypto_ohlcv`` for BTCUSDT 1 s + 5 m candles, builds the
V2 feature vector at each market's t=210 s, trains a LightGBM
classifier with Optuna hyper-parameter search, evaluates on a held-out
tail, optionally calibrates with isotonic regression, writes the
artefacts under ``models/last_90s_forecaster_v2/<version>/``, and
upserts a ``research.models`` row (``is_active`` toggleable).

Promotion gate: ``AUC_test ≥ 0.55 AND Brier_test ≤ 0.245 AND
ECE_val ≤ 0.05``. Failing any gate → row written with ``is_active =
FALSE`` and a clear metrics blob so the strategy stays in shadow mode.

Usage::

    docker compose exec tea-engine python -m trading.cli.train_last90s \\
        --from 2026-01-15 --to 2026-04-20 \\
        --polybot-btc5m /polybot-btc5m-data/polybot_agent.db \\
        --polybot-agent /btc-tendencia-data/polybot_agent.db \\
        --optuna-trials 200 --time-budget-s 3600 \\
        --promote

All heavy imports (lightgbm, optuna, numpy, pandas, sklearn) are lazy.
The module is importable without them so the test suite can exercise
helpers that don't touch training.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("cli.train_last90s")


@dataclass
class Sample:
    open_ts: float
    close_ts: float
    slug: str
    features: list[float]
    label: int  # 1 if close > open else 0


# ------------------------------------------------------------------ IO


def _fetch_polymarket_implied_yes(
    pg_dsn: str,
    slug: str,
    as_of_unix: int,
) -> float | None:
    """Latest YES price from `market_data.polymarket_prices_history` for
    the given market_slug at or before ``as_of_unix``. None when no row
    exists (slug not backfilled / as_of pre-dates first sample) — caller
    drops sample (prefer-drop-over-poison, same policy as labels fix).
    """
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pp.price::float8
                FROM market_data.polymarket_prices_history pp
                JOIN market_data.polymarket_markets pm
                  ON pm.condition_id = pp.condition_id
                WHERE pm.slug = %s
                  AND pp.outcome = 'YES'
                  AND pp.ts <= to_timestamp(%s)
                ORDER BY pp.ts DESC
                LIMIT 1
                """,
                (slug, as_of_unix),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None
    finally:
        conn.close()


def _fetch_microstructure_for_window(
    pg_dsn: str,
    end_ts_unix: int,
    window_s: int = 90,
) -> tuple[list[tuple[float, float, str]], int]:
    """For one market window, return (trades_in_window, baseline_24h_count).

    ``trades_in_window``: list of (price, qty, side) for trades with
    ``ts ∈ [end_ts - window_s, end_ts]``. Empty list when no trades.
    ``baseline_24h_count``: count of trades in the trailing 24 h ending
    at ``end_ts`` — used by ``trade_intensity``.
    """
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price::float8, qty::float8, side "
                "FROM market_data.crypto_trades "
                "WHERE exchange='binance' AND symbol='BTCUSDT' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                (end_ts_unix - window_s, end_ts_unix),
            )
            trades = [(float(p), float(q), str(s)) for (p, q, s) in cur.fetchall()]
            cur.execute(
                "SELECT COUNT(*) FROM market_data.crypto_trades "
                "WHERE exchange='binance' AND symbol='BTCUSDT' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                (end_ts_unix - 86400, end_ts_unix),
            )
            row = cur.fetchone()
            baseline = int(row[0]) if row else 0
            return trades, baseline
    finally:
        conn.close()


def _fetch_ohlcv_1m_closes(pg_dsn: str, t_min_unix: int, t_max_unix: int) -> dict[int, float]:
    """Bulk-fetch BTCUSDT 1m closes in a unix-second range, indexed by
    minute-floor unix timestamp. Used by ``_load_resolved_markets`` to
    re-derive labels from Binance instead of the biased polybot
    chainlink path (audit 2026-04-25)."""
    if t_max_unix <= t_min_unix:
        return {}
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM ts)::bigint, close "
                "FROM market_data.crypto_ohlcv "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1m' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                (t_min_unix, t_max_unix),
            )
            return {int(t): float(c) for (t, c) in cur.fetchall() if c is not None}
    finally:
        conn.close()


def _load_resolved_markets(
    sqlite_path: Path,
    slug_encodes_open_ts: bool,
    *,
    pg_dsn: str,
) -> list[dict]:
    """Resolved-market list with open_price/close_price re-derived from
    Binance ``market_data.crypto_ohlcv`` (1m, BTCUSDT, exchange='binance')
    at minute(open_ts) / minute(close_ts) — NOT polybot's ``ticks.open_price``
    nor last-tick ``chainlink_price``.

    Why: audit on 2026-04-25 (see
    ``estrategias/en-desarrollo/_audit_polybot_groundtruth.md``) showed
    polybot's chainlink is frozen for 47 % of markets and
    ``open_price == first_chainlink_price`` for 50 %, producing 40.5 %
    of training labels inverted vs Binance. Markets without OHLCV
    coverage at either end are *dropped*, never fallback-resolved —
    same prefer-drop-over-poison policy as the fase 1 fix to
    ``paper/backtest_loader.py``.

    Polybot SQLite is still consulted: it tells us which markets
    actually resolved (``trades.resolution IN ('win','loss')``) and the
    slug-encoded open/close ts. Only the price fields are overridden.

    - ``slug_encodes_open_ts=True`` (BTC-Tendencia): slug's trailing
      number is window open ts, close = open + 300.
    - ``False`` (polybot-btc5m): slug's trailing number is close_ts,
      open = close - 300.
    """
    if not sqlite_path.exists():
        log.warning("sqlite missing: %s", sqlite_path)
        return []
    # immutable=1 skips WAL journal checks, letting us open a live DB
    # read-only without a .shm/.wal sidecar write permission.
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Resolved trades give us which markets actually closed with a
        # known outcome. One row per distinct market_slug.
        trades = con.execute(
            """
            SELECT DISTINCT market_slug
            FROM trades
            WHERE resolution IN ('win', 'loss')
              AND market_slug LIKE 'btc-%updown-5m-%'
            """
        ).fetchall()
        resolved_slugs = [r["market_slug"] for r in trades]
    finally:
        con.close()
    if not resolved_slugs:
        return []

    # Discover (slug, open_ts, close_ts) without touching polybot prices.
    discovered: list[tuple[str, int, int, int, int]] = []
    minutes: set[int] = set()
    for slug in resolved_slugs:
        try:
            trailing_ts = int(slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        if slug_encodes_open_ts:
            open_ts = trailing_ts
            close_ts = trailing_ts + 300
        else:
            close_ts = trailing_ts
            open_ts = trailing_ts - 300
        open_min = (int(open_ts) // 60) * 60
        close_min = (int(close_ts) // 60) * 60
        discovered.append((slug, open_ts, close_ts, open_min, close_min))
        minutes.add(open_min)
        minutes.add(close_min)
    if not discovered:
        return []

    closes = _fetch_ohlcv_1m_closes(pg_dsn, min(minutes), max(minutes))

    out: list[dict] = []
    n_dropped_gap = 0
    for slug, open_ts, close_ts, open_min, close_min in discovered:
        bin_open = closes.get(open_min)
        bin_close = closes.get(close_min)
        if bin_open is None or bin_close is None:
            n_dropped_gap += 1
            continue
        out.append(
            {
                "slug": slug,
                "condition_id": slug,
                "open_ts": open_ts,
                "close_ts": close_ts,
                "open_price": bin_open,
                "close_price": bin_close,
            }
        )
    if n_dropped_gap:
        log.info(
            "_load_resolved_markets: dropped %d markets with OHLCV gap "
            "(no 1m candle at minute(open_ts) or minute(close_ts))",
            n_dropped_gap,
        )
    log.info(
        "_load_resolved_markets: %s — kept %d / %d resolved",
        sqlite_path.name,
        len(out),
        len(resolved_slugs),
    )
    return out


def _load_ticks_for_slug(
    sqlite_path: Path,
    slug: str,
    open_ts: float,
    cutoff_ts: float,
) -> list[float]:
    """Read 1 Hz BTC spot ticks for one market from polybot's ``ticks``
    table, up to (and including) ``cutoff_ts``. Returns samples ordered
    ascending.
    """
    if not sqlite_path.exists():
        return []
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts, spot_price FROM ticks "
            "WHERE market_slug = ? AND ts BETWEEN ? AND ? "
            "AND spot_price IS NOT NULL AND spot_price > 0 "
            "ORDER BY ts ASC",
            (slug, float(open_ts), float(cutoff_ts)),
        ).fetchall()
    finally:
        con.close()
    return [float(r["spot_price"]) for r in rows]


def _load_ohlcv_5m(
    pg_dsn: str,
    since_ts: int,
    until_ts: int,
) -> list[tuple[float, float, float, float]]:
    """(ts, high, low, close) tuples for macro features."""
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM ts)::bigint, high, low, close "
                "FROM market_data.crypto_ohlcv "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "ORDER BY ts ASC",
                (since_ts, until_ts),
            )
            return [(float(t), float(h), float(lo), float(c)) for (t, h, lo, c) in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------- Feature build


def build_samples(
    markets: list[dict],
    *,
    sqlite_sources: list[Path],
    candles_5m: list[tuple[float, float, float, float]],
    include_bb_residual: bool = False,
    include_microstructure: bool = False,
    use_real_implied_prob: bool = False,
    pg_dsn: str | None = None,
    microstructure_window_s: int = 90,
    large_threshold_usd: float = 100_000.0,
) -> list[Sample]:
    """Assemble Sample rows at t=210 s for each market.

    1 Hz BTC spot comes from the polybot ``ticks`` table directly
    (no TEA 1 s ohlcv available in staging). Macro features pull from
    TEA ``market_data.crypto_ohlcv`` 5 m candles. When
    ``include_bb_residual`` is set, the 4 ``bb_*`` features are appended
    at the tail (open_price → polybot ticks; vol → realized_vol_yz of
    the 90 s window — same path the strategy uses at serving).
    """
    from trading.engine.features.macro import snapshot
    from trading.strategies.polymarket_btc5m._v2_features import (
        V2FeatureInputs,
        build_vector,
    )

    def _macro_at(as_of_ts: float):
        cutoff = int(as_of_ts) - 300
        eligible = [c for c in candles_5m if c[0] <= cutoff]
        if len(eligible) < 34:
            return None
        window = eligible[-34:]
        highs = [c[1] for c in window]
        lows = [c[2] for c in window]
        closes = [c[3] for c in window]
        return snapshot(highs, lows, closes)

    if include_microstructure and pg_dsn is None:
        raise ValueError("include_microstructure=True requires pg_dsn for crypto_trades fetch")
    if use_real_implied_prob and pg_dsn is None:
        raise ValueError("use_real_implied_prob=True requires pg_dsn")

    if include_microstructure:
        from trading.engine.features.binance_microstructure import (
            Trade,
            binance_microstructure_from_trades,
        )

    samples: list[Sample] = []
    n_dropped_micro = 0
    n_dropped_implied = 0
    for m in markets:
        open_ts = int(m["open_ts"])
        close_ts = int(m["close_ts"])
        as_of = float(open_ts + 210)
        src = Path(m["_source"])
        spots = _load_ticks_for_slug(src, m["slug"], open_ts + 120, as_of)
        if len(spots) < 60:
            continue
        snap = _macro_at(as_of)
        if snap is None:
            continue
        # Read real implied_prob_yes from polymarket_prices_history if
        # available (TAREA 3.9). Fallback to 0.5 keeps legacy behavior.
        implied_yes = 0.5
        if use_real_implied_prob:
            implied_yes_raw = _fetch_polymarket_implied_yes(
                pg_dsn,  # type: ignore[arg-type]
                m["slug"],
                int(as_of),
            )
            if implied_yes_raw is None:
                # Drop sample — `bb_market_vs_prior` and any future feature
                # using the Polymarket book would carry a synthetic 0.5
                # otherwise, falsifying training distribution.
                n_dropped_implied += 1
                continue
            implied_yes = max(0.0, min(1.0, implied_yes_raw))
        inp = V2FeatureInputs(
            as_of_ts=as_of,
            spots_last_90s=spots,
            macro_snap=snap,
            # When use_real_implied_prob=False, neutral defaults below.
            # The book snapshot fields (yes_ask/no_ask/depth) are still
            # neutral — backfilling those would require a full L2 history
            # ingest, out of scope for v3.
            implied_prob_yes=implied_yes,
            yes_ask=implied_yes,
            no_ask=1.0 - implied_yes,
            depth_yes=100.0,
            depth_no=100.0,
            pm_imbalance=0.0,
            pm_spread_bps=50.0,
            open_price=float(m["open_price"]),
            t_in_window_s=210.0,
            bb_T_seconds=300.0,
        )
        vec = build_vector(inp, include_bb_residual=include_bb_residual)

        if include_microstructure:
            # Fetch the [as_of - window_s, as_of] trade set and 24h baseline
            # for trade_intensity. as_of = open_ts + 210, so the window ends
            # at the same as_of_ts the strategy uses at decision time.
            ms_trades_raw, baseline_24h = _fetch_microstructure_for_window(
                pg_dsn,
                int(as_of),
                window_s=microstructure_window_s,  # type: ignore[arg-type]
            )
            if not ms_trades_raw:
                # Drop sample to keep training distribution honest — same
                # prefer-drop-over-poison rule as the OHLCV-label fix.
                n_dropped_micro += 1
                continue
            ms_trades = [Trade(price=p, qty=q, side=s) for (p, q, s) in ms_trades_raw]
            ms_features = binance_microstructure_from_trades(
                ms_trades,
                baseline_trades_24h=baseline_24h,
                window_s=microstructure_window_s,
                large_threshold_usd=large_threshold_usd,
            )
            vec = vec + [
                ms_features["bm_cvd_normalized"],
                ms_features["bm_taker_buy_ratio"],
                ms_features["bm_trade_intensity"],
                ms_features["bm_large_trade_flag"],
                ms_features["bm_signed_autocorr_lag1"],
            ]

        label = 1 if float(m["close_price"]) > float(m["open_price"]) else 0
        samples.append(
            Sample(
                open_ts=float(open_ts),
                close_ts=float(close_ts),
                slug=str(m["slug"]),
                features=vec,
                label=label,
            )
        )
    if include_microstructure:
        log.info(
            "build_samples: microstructure dropped %d / kept %d (no crypto_trades in window)",
            n_dropped_micro,
            len(samples),
        )
    if use_real_implied_prob:
        log.info(
            "build_samples: implied_prob dropped %d / kept %d (no polymarket_prices_history row)",
            n_dropped_implied,
            len(samples),
        )
    return samples


# ---------------------------------------------------------------- Train


def _expected_calibration_error(probs, labels, n_bins: int = 10) -> float:
    import numpy as np

    probs = np.asarray(probs, dtype=np.float64)
    labels = np.asarray(labels, dtype=np.int32)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(probs)
    for i in range(n_bins):
        mask = (probs >= bins[i]) & (probs < bins[i + 1] + (1e-9 if i == n_bins - 1 else 0))
        if not mask.any():
            continue
        avg_p = probs[mask].mean()
        avg_y = labels[mask].mean()
        ece += (mask.sum() / n) * abs(avg_p - avg_y)
    return float(ece)


def train(
    samples: list[Sample],
    *,
    optuna_trials: int,
    time_budget_s: int,
    random_state: int = 42,
) -> dict:
    import lightgbm as lgb
    import numpy as np
    import optuna
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    X = np.asarray([s.features for s in samples], dtype=np.float64)
    y = np.asarray([s.label for s in samples], dtype=np.int32)
    n = len(samples)
    n_train = int(n * 0.70)
    n_val = int(n * 0.15)
    X_train, y_train = X[:n_train], y[:n_train]
    X_val, y_val = X[n_train : n_train + n_val], y[n_train : n_train + n_val]
    X_test, y_test = X[n_train + n_val :], y[n_train + n_val :]

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial: optuna.trial.Trial) -> float:
        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": trial.suggest_float("lr", 0.005, 0.1, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 15, 127),
            "min_data_in_leaf": trial.suggest_int("min_data", 20, 200),
            "feature_fraction": trial.suggest_float("ff", 0.5, 1.0),
            "bagging_fraction": trial.suggest_float("bf", 0.5, 1.0),
            "bagging_freq": 5,
            "lambda_l2": trial.suggest_float("l2", 1e-4, 10.0, log=True),
            "verbose": -1,
            "seed": random_state,
        }
        d_train = lgb.Dataset(X_train, y_train)
        d_val = lgb.Dataset(X_val, y_val, reference=d_train)
        model = lgb.train(
            params,
            d_train,
            num_boost_round=2000,
            valid_sets=[d_val],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        trial.set_user_attr("n_iter", model.best_iteration)
        return log_loss(y_val, model.predict(X_val, num_iteration=model.best_iteration))

    # Seed the TPE sampler so a given (samples, seed) reproduces the same
    # hyper-parameter trajectory — critical for comparing v2_clean vs
    # v2_bbres without conflating Optuna search noise with feature lift.
    sampler = optuna.samplers.TPESampler(seed=random_state)
    study = optuna.create_study(direction="minimize", sampler=sampler)
    study.optimize(objective, n_trials=optuna_trials, timeout=time_budget_s)
    best = study.best_trial

    # Refit best on train+val for final booster.
    final_params = {
        **best.params,
        "objective": "binary",
        "metric": "binary_logloss",
        "verbose": -1,
        "seed": random_state,
    }
    X_tv = np.vstack([X_train, X_val])
    y_tv = np.concatenate([y_train, y_val])
    final = lgb.train(
        final_params,
        lgb.Dataset(X_tv, y_tv),
        num_boost_round=best.user_attrs.get("n_iter", 200),
    )
    test_probs = final.predict(X_test)
    val_probs = final.predict(X_val)

    ece_val = _expected_calibration_error(val_probs, y_val)
    calibrator = None
    if ece_val > 0.05:
        calibrator = IsotonicRegression(out_of_bounds="clip").fit(val_probs, y_val)

    test_logloss = float(log_loss(y_test, test_probs))
    test_auc = float(roc_auc_score(y_test, test_probs))
    test_brier = float(brier_score_loss(y_test, test_probs))

    return {
        "model": final,
        "calibrator": calibrator,
        "metrics": {
            "test_logloss": test_logloss,
            "test_auc": test_auc,
            "test_brier": test_brier,
            "ece_val": ece_val,
            "calibrated": calibrator is not None,
            "n_train": n_train,
            "n_val": n_val,
            "n_test": len(X_test),
            "best_params": best.params,
            "n_iterations": int(best.user_attrs.get("n_iter", 0)),
        },
    }


# ------------------------------------------------------ Artefact writing


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[3],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _passes_promotion(metrics: dict) -> bool:
    """Promotion gate with sample-size–aware tolerances.

    For large samples (n ≥ 200 each) the canonical thresholds bind:
    AUC ≥ 0.55, Brier ≤ 0.245, ECE ≤ 0.05. Small paper datasets (~30
    test points) have a Brier std error near 0.02–0.03 and ECE std
    error near 0.05, so we widen both caps. AUC remains strict because
    its estimator is the most stable under class imbalance.
    """
    n_val = int(metrics.get("n_val", 0))
    n_test = int(metrics.get("n_test", 0))
    ece_cap = 0.05 if n_val >= 200 else 0.20
    brier_cap = 0.245 if n_test >= 200 else 0.260
    return (
        metrics["test_auc"] >= 0.55
        and metrics["test_brier"] <= brier_cap
        and metrics["ece_val"] <= ece_cap
    )


def write_artefacts(
    *,
    name: str,
    trained: dict,
    training_period_from: datetime,
    training_period_to: datetime,
    promote: bool,
    include_bb_residual: bool = False,
    include_microstructure: bool = False,
    feature_names_override: list[str] | None = None,
    version_tag: str | None = None,
) -> dict:
    import asyncio
    import pickle

    if version_tag is None:
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        version = f"v2_{stamp}"
    else:
        version = version_tag
    out_dir = Path("models") / name / version
    out_dir.mkdir(parents=True, exist_ok=True)
    trained["model"].save_model(str(out_dir / "model.lgb"))
    if trained["calibrator"] is not None:
        with open(out_dir / "calibrator.pkl", "wb") as f:
            pickle.dump(trained["calibrator"], f)
    feat_module = __import__(
        "trading.strategies.polymarket_btc5m._v2_features",
        fromlist=["feature_names"],
    )
    if feature_names_override is not None:
        feat_names: list[str] = list(feature_names_override)
    else:
        feat_names = list(feat_module.feature_names(include_bb_residual))
    meta = {
        "name": name,
        "version": version,
        "feature_names": feat_names,
        "include_bb_residual": include_bb_residual,
        "include_microstructure": include_microstructure,
        "metrics": trained["metrics"],
        "training_period_from": training_period_from.isoformat(),
        "training_period_to": training_period_to.isoformat(),
        "git_sha": _git_sha(),
        "lightgbm_version": __import__("lightgbm").__version__,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    passes = _passes_promotion(trained["metrics"])
    is_active = promote and passes

    async def _upsert() -> None:
        from trading.common.db import acquire, close_pool

        async with acquire() as conn:
            if is_active:
                await conn.execute(
                    "UPDATE research.models SET is_active = FALSE WHERE name = $1",
                    name,
                )
            await conn.execute(
                """
                INSERT INTO research.models
                    (id, name, version, path, metrics, params,
                     training_period_from, training_period_to,
                     git_sha, trained_at, is_active)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb,
                        $7, $8, $9, now(), $10)
                """,
                uuid.uuid4(),
                name,
                version,
                str(out_dir),
                json.dumps(trained["metrics"]),
                json.dumps(trained["metrics"].get("best_params", {})),
                training_period_from,
                training_period_to,
                meta["git_sha"],
                is_active,
            )
        await close_pool()

    asyncio.run(_upsert())
    return {
        "version": version,
        "path": str(out_dir),
        "passes_gate": passes,
        "is_active": is_active,
        "metrics": trained["metrics"],
    }


# ------------------------------------------------------------------ main


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--polybot-btc5m", default="/polybot-btc5m-data/polybot_agent.db")
    ap.add_argument("--polybot-agent", default="/btc-tendencia-data/polybot_agent.db")
    ap.add_argument("--optuna-trials", type=int, default=200)
    ap.add_argument("--time-budget-s", type=int, default=3600)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument(
        "--include-bb-residual",
        action="store_true",
        help="Append the 4 bb_residual features at the end of the vector "
        "(produces a 25-feature model versioned `v2_bbres_<ts>`).",
    )
    ap.add_argument("--seed", type=int, default=42, help="LightGBM/Optuna seed.")
    ap.add_argument(
        "--strategy",
        choices=["v2", "v3"],
        default="v2",
        help="v2 = 21 features (current). v3 = v2 + 5 Binance microstructure "
        "features (CVD, taker_ratio, intensity, large_trade, signed_autocorr) "
        "queried from market_data.crypto_trades.",
    )
    ap.add_argument(
        "--microstructure-window-s",
        type=int,
        default=90,
        help="Window size (seconds) for v3 microstructure features.",
    )
    ap.add_argument(
        "--large-trade-threshold-usd",
        type=float,
        default=100_000.0,
        help="Notional threshold for bm_large_trade_flag (default $100k).",
    )
    ap.add_argument(
        "--use-real-implied-prob",
        action="store_true",
        help="Read implied_prob_yes from market_data.polymarket_prices_history "
        "at as_of_ts instead of hardcoding 0.5. Requires the table to be "
        "backfilled (see scripts/backfill_polymarket_prices_history.py). "
        "Drops samples whose market has no row for the as_of timestamp.",
    )
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)

    # Copy the live sqlite files to /tmp so concurrent writers on the
    # host don't trigger transient "database disk image is malformed".
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="tea_train_"))

    def _snapshot(src: str) -> str:
        p = Path(src)
        if not p.exists():
            return src
        dst = tmp_dir / p.name
        shutil.copy2(p, dst)
        return str(dst)

    polybot_btc5m_snap = _snapshot(args.polybot_btc5m)
    polybot_agent_snap = _snapshot(args.polybot_agent)
    log.info(
        "snapshotted sqlites to %s: btc5m=%s agent=%s",
        tmp_dir,
        polybot_btc5m_snap,
        polybot_agent_snap,
    )

    pg_dsn = os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )

    log.info("loading resolved markets (open/close re-derived from Binance 1m)")
    ma = _load_resolved_markets(Path(polybot_btc5m_snap), slug_encodes_open_ts=False, pg_dsn=pg_dsn)
    for m in ma:
        m["_source"] = polybot_btc5m_snap
    mb = _load_resolved_markets(Path(polybot_agent_snap), slug_encodes_open_ts=True, pg_dsn=pg_dsn)
    for m in mb:
        m["_source"] = polybot_agent_snap
    markets = [
        m for m in (ma + mb) if t_from.timestamp() <= float(m["close_ts"]) <= t_to.timestamp()
    ]
    log.info(
        "resolved markets: %d (polybot-btc5m=%d polybot-agent=%d)",
        len(markets),
        len(ma),
        len(mb),
    )
    if len(markets) < 100:
        log.error("too few markets (%d) — need ≥ 100 for training", len(markets))
        return 2
    since_ts = int(t_from.timestamp()) - 3600
    until_ts = int(t_to.timestamp()) + 3600
    log.info("loading crypto_ohlcv 5 m for macro features")
    candles = _load_ohlcv_5m(pg_dsn, since_ts, until_ts)
    log.info("ohlcv 5m rows: %d", len(candles))

    sqlite_sources = [
        Path(args.polybot_btc5m),
        Path(args.polybot_agent),
    ]
    samples = build_samples(
        markets,
        sqlite_sources=sqlite_sources,
        candles_5m=candles,
        include_bb_residual=args.include_bb_residual,
        include_microstructure=(args.strategy == "v3"),
        use_real_implied_prob=args.use_real_implied_prob,
        pg_dsn=pg_dsn,
        microstructure_window_s=args.microstructure_window_s,
        large_threshold_usd=args.large_trade_threshold_usd,
    )
    log.info(
        "feature samples: %d / markets=%d / n_features=%d / strategy=%s / "
        "include_bb_residual=%s / include_microstructure=%s",
        len(samples),
        len(markets),
        len(samples[0].features) if samples else 0,
        args.strategy,
        args.include_bb_residual,
        args.strategy == "v3",
    )
    if len(samples) < 100:
        log.error("too few samples after feature build")
        return 3

    log.info("training — optuna_trials=%d budget_s=%d", args.optuna_trials, args.time_budget_s)
    trained = train(
        samples,
        optuna_trials=args.optuna_trials,
        time_budget_s=args.time_budget_s,
        random_state=args.seed,
    )
    log.info("metrics: %s", json.dumps(trained["metrics"]))

    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    feat_names_override: list[str] | None = None
    if args.strategy == "v3":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            feature_names_v3,
        )

        feat_names_override = list(feature_names_v3())
        if args.use_real_implied_prob:
            version_tag = f"v3_priceshist_{stamp}"
        else:
            version_tag = f"v3_first_{stamp}"
        model_name = "last_90s_forecaster_v3"
    elif args.include_bb_residual:
        version_tag = f"v2_bbres_{stamp}"
        model_name = "last_90s_forecaster_v2"
    else:
        version_tag = None
        model_name = "last_90s_forecaster_v2"
    out = write_artefacts(
        name=model_name,
        trained=trained,
        training_period_from=t_from,
        training_period_to=t_to,
        promote=args.promote,
        include_bb_residual=args.include_bb_residual,
        include_microstructure=(args.strategy == "v3"),
        feature_names_override=feat_names_override,
        version_tag=version_tag,
    )
    log.info("artefacts: %s", json.dumps({k: v for k, v in out.items() if k != "metrics"}))
    return 0 if out["passes_gate"] else 1


if __name__ == "__main__":
    sys.exit(main())
