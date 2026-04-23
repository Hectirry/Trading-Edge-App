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


def _load_resolved_markets(sqlite_path: Path, slug_encodes_open_ts: bool) -> list[dict]:
    """Read resolved btc-updown-5m markets from one polybot-style DB.

    The two siblings have slightly different slug conventions; the flag
    comes from the existing PolybotSQLiteLoader defaults.
    """
    if not sqlite_path.exists():
        log.warning("sqlite missing: %s", sqlite_path)
        return []
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT slug, condition_id, close_ts, open_ts, outcome,
                   open_price, close_price
            FROM markets
            WHERE resolved = 1
              AND slug LIKE 'btc-%updown-5m-%'
              AND open_price IS NOT NULL AND close_price IS NOT NULL
            ORDER BY close_ts ASC
            """
        ).fetchall()
    finally:
        con.close()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if slug_encodes_open_ts and d.get("open_ts") is None:
            # polybot-agent: the slug's trailing number IS open_ts.
            try:
                d["open_ts"] = int(d["slug"].rsplit("-", 1)[-1])
            except Exception:
                continue
        if not d.get("open_ts"):
            # polybot-btc5m: slug encodes close_ts; recover open_ts.
            d["open_ts"] = int(d["close_ts"]) - 300
        out.append(d)
    return out


def _load_ohlcv_1s(pg_dsn: str, since_ts: int, until_ts: int):
    """Binance BTCUSDT 1 s candles over [since, until] for micro features.

    We pull into a dict ``ts -> close`` rather than a DataFrame to keep
    the dependency surface small. Caller builds per-market ``spots``
    slices from this dict.
    """
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM ts)::bigint, close "
                "FROM market_data.crypto_ohlcv "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='1s' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "ORDER BY ts ASC",
                (since_ts, until_ts),
            )
            return {int(t): float(c) for (t, c) in cur.fetchall()}
    finally:
        conn.close()


def _load_ohlcv_5m(
    pg_dsn: str, since_ts: int, until_ts: int,
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
            return [
                (float(t), float(h), float(lo), float(c)) for (t, h, lo, c) in cur.fetchall()
            ]
    finally:
        conn.close()


# ---------------------------------------------------------- Feature build


def build_samples(
    markets: list[dict],
    *,
    ohlcv_1s: dict[int, float],
    candles_5m: list[tuple[float, float, float, float]],
) -> list[Sample]:
    """Assemble Sample rows at t=210 s for each market."""
    from trading.engine.features.macro import snapshot
    from trading.strategies.polymarket_btc5m._v2_features import (
        FEATURE_NAMES,  # noqa: F401  -- kept to pin ordering at import time
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

    samples: list[Sample] = []
    for m in markets:
        open_ts = int(m["open_ts"])
        close_ts = int(m["close_ts"])
        as_of = float(open_ts + 210)
        spots = [
            ohlcv_1s[ts] for ts in range(open_ts + 120, int(as_of) + 1) if ts in ohlcv_1s
        ]
        if len(spots) < 60:
            continue
        snap = _macro_at(as_of)
        if snap is None:
            continue
        inp = V2FeatureInputs(
            as_of_ts=as_of,
            spots_last_90s=spots,
            macro_snap=snap,
            # The training-time book snapshot isn't reliably in the polybot
            # DBs; we feed neutral defaults. The model learns from micro +
            # macro + time features primarily.
            implied_prob_yes=0.5,
            yes_ask=0.5, no_ask=0.5,
            depth_yes=100.0, depth_no=100.0,
            pm_imbalance=0.0, pm_spread_bps=50.0,
        )
        vec = build_vector(inp)
        label = 1 if float(m["close_price"]) > float(m["open_price"]) else 0
        samples.append(Sample(
            open_ts=float(open_ts), close_ts=float(close_ts),
            slug=str(m["slug"]), features=vec, label=label,
        ))
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
            params, d_train, num_boost_round=2000, valid_sets=[d_val],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        trial.set_user_attr("n_iter", model.best_iteration)
        return log_loss(y_val, model.predict(X_val, num_iteration=model.best_iteration))

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=optuna_trials, timeout=time_budget_s)
    best = study.best_trial

    # Refit best on train+val for final booster.
    final_params = {
        **best.params,
        "objective": "binary", "metric": "binary_logloss",
        "verbose": -1, "seed": random_state,
    }
    X_tv = np.vstack([X_train, X_val])
    y_tv = np.concatenate([y_train, y_val])
    final = lgb.train(
        final_params, lgb.Dataset(X_tv, y_tv),
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
            "n_train": n_train, "n_val": n_val, "n_test": len(X_test),
            "best_params": best.params,
            "n_iterations": int(best.user_attrs.get("n_iter", 0)),
        },
    }


# ------------------------------------------------------ Artefact writing


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _passes_promotion(metrics: dict) -> bool:
    return (
        metrics["test_auc"] >= 0.55
        and metrics["test_brier"] <= 0.245
        and metrics["ece_val"] <= 0.05
    )


def write_artefacts(
    *,
    name: str,
    trained: dict,
    training_period_from: datetime,
    training_period_to: datetime,
    promote: bool,
) -> dict:
    import asyncio
    import pickle

    version = f"v2_{datetime.now(tz=UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}"
    out_dir = Path("models") / name / version
    out_dir.mkdir(parents=True, exist_ok=True)
    trained["model"].save_model(str(out_dir / "model.lgb"))
    if trained["calibrator"] is not None:
        with open(out_dir / "calibrator.pkl", "wb") as f:
            pickle.dump(trained["calibrator"], f)
    meta = {
        "name": name,
        "version": version,
        "feature_names": list(__import__(
            "trading.strategies.polymarket_btc5m._v2_features",
            fromlist=["FEATURE_NAMES"],
        ).FEATURE_NAMES),
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
                uuid.uuid4(), name, version, str(out_dir),
                json.dumps(trained["metrics"]),
                json.dumps(trained["metrics"].get("best_params", {})),
                training_period_from, training_period_to,
                meta["git_sha"], is_active,
            )
        await close_pool()

    asyncio.run(_upsert())
    return {
        "version": version, "path": str(out_dir),
        "passes_gate": passes, "is_active": is_active,
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
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)

    log.info("loading resolved markets")
    ma = _load_resolved_markets(Path(args.polybot_btc5m), slug_encodes_open_ts=False)
    mb = _load_resolved_markets(Path(args.polybot_agent), slug_encodes_open_ts=True)
    markets = [
        m for m in (ma + mb)
        if t_from.timestamp() <= float(m["close_ts"]) <= t_to.timestamp()
    ]
    log.info(
        "resolved markets: %d (polybot-btc5m=%d polybot-agent=%d)",
        len(markets), len(ma), len(mb),
    )
    if len(markets) < 200:
        log.error("too few markets (%d) — need ≥ 200 for a stable split", len(markets))
        return 2

    pg_dsn = os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )
    since_ts = int(t_from.timestamp()) - 3600
    until_ts = int(t_to.timestamp()) + 3600
    log.info("loading crypto_ohlcv 1 s + 5 m")
    spots = _load_ohlcv_1s(pg_dsn, since_ts, until_ts)
    candles = _load_ohlcv_5m(pg_dsn, since_ts, until_ts)
    log.info("ohlcv rows: 1s=%d 5m=%d", len(spots), len(candles))

    samples = build_samples(markets, ohlcv_1s=spots, candles_5m=candles)
    log.info("feature samples: %d", len(samples))
    if len(samples) < 200:
        log.error("too few samples after feature build")
        return 3

    log.info("training — optuna_trials=%d budget_s=%d", args.optuna_trials, args.time_budget_s)
    trained = train(samples, optuna_trials=args.optuna_trials,
                    time_budget_s=args.time_budget_s)
    log.info("metrics: %s", json.dumps(trained["metrics"]))

    out = write_artefacts(
        name="last_90s_forecaster_v2",
        trained=trained,
        training_period_from=t_from,
        training_period_to=t_to,
        promote=args.promote,
    )
    log.info("artefacts: %s", json.dumps({k: v for k, v in out.items() if k != "metrics"}))
    return 0 if out["passes_gate"] else 1


if __name__ == "__main__":
    sys.exit(main())
