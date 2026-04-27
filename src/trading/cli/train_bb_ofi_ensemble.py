"""Bootstrap-ensemble trainer for ``bb_residual_ofi_v1``.

Reuses ``build_samples`` from :mod:`trading.cli.train_bb_ofi` (one heavy
I/O pass, ~5 min). Then trains *N* LightGBM members where each member
sees a different bootstrap resample of the train+val partition, with
its own Optuna study seed. The held-out test partition is **fixed**
across members — so test-set predictions are directly poolable.

Outputs:

* ``models/bb_residual_ofi_v1/<version>/member_<i>/model.lgb`` — N members.
* ``models/bb_residual_ofi_v1/<version>/calibrator.pkl`` — single isotonic
  calibrator fit on the ensemble's val-set mean prediction.
* ``models/bb_residual_ofi_v1/<version>/meta.json`` — ensemble metrics
  (AUC, Brier, ECE post-calibration) plus per-member metrics and the
  distribution of per-prediction stddev on test.

Why bootstrap not just multi-seed: bagging averages predictions of
models trained on overlapping but distinct subsets, which is the
canonical way to (a) reduce variance on imbalanced binary problems
and (b) get a meaningful per-prediction stddev — multi-seed alone
mostly varies Optuna trajectories and LightGBM column subsampling,
which underestimates the true predictive uncertainty.

Usage::

    docker compose exec tea-engine python -m trading.cli.train_bb_ofi_ensemble \\
        --from 2026-03-23 --to 2026-04-21 \\
        --members 7 --optuna-trials 40 --time-budget-s 600 \\
        --use-real-implied-prob --promote
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from trading.cli.train_bb_ofi import (
    _load_settled_markets_from_pg,
    build_samples,
)
from trading.cli.train_last90s import (
    _expected_calibration_error,
    _load_resolved_markets,
    _passes_promotion,
)
from trading.strategies.polymarket_btc5m._bb_ofi_features import FEATURE_NAMES

log = logging.getLogger("cli.train_bb_ofi_ensemble")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


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


def _train_member(
    *,
    X_train_val,
    y_train_val,
    X_val,
    y_val,
    optuna_trials: int,
    time_budget_s: int,
    seed: int,
):
    """Train one LightGBM booster with Optuna over (X_train_val, y_train_val)
    using X_val as the early-stopping set. Returns the booster + best params.
    """
    import lightgbm as lgb
    import optuna
    from sklearn.metrics import log_loss

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
            "seed": seed,
        }
        d_train = lgb.Dataset(X_train_val, y_train_val)
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

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="minimize", sampler=sampler)
    study.optimize(objective, n_trials=optuna_trials, timeout=time_budget_s)
    best = study.best_trial

    final_params = {
        **best.params,
        "objective": "binary",
        "metric": "binary_logloss",
        "verbose": -1,
        "seed": seed,
    }
    final = lgb.train(
        final_params,
        lgb.Dataset(X_train_val, y_train_val),
        num_boost_round=best.user_attrs.get("n_iter", 200),
    )
    return final, dict(best.params), int(best.user_attrs.get("n_iter", 0))


def _train_ensemble(
    samples: list,
    *,
    n_members: int,
    optuna_trials: int,
    time_budget_s: int,
    base_seed: int = 42,
) -> dict:
    """Train an N-member bagging ensemble. Splits 70/15/15 train/val/test
    using the original sample order (chronological — same convention as
    ``train_last90s.train``). Each member sees a bootstrap resample of
    *train only* (with replacement, n=n_train); val + test stay fixed
    across members. The test set is fixed for cross-member comparison;
    val stays out of the bootstrap so early-stopping is honest.

    Returns dict with: ``members`` (list of (booster, best_params,
    n_iter)), ``calibrator`` (isotonic on ensemble val-mean), ``metrics``
    (test-set ensemble metrics post-calibration), ``test_stddev_summary``
    (distribution of per-prediction stddev).
    """
    import numpy as np
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    from trading.research.calibration import fit_platt

    X = np.asarray([s.features for s in samples], dtype=np.float64)
    y = np.asarray([s.label for s in samples], dtype=np.int32)
    n = len(samples)
    n_train = int(n * 0.70)
    n_val = int(n * 0.15)
    X_train = X[:n_train]
    y_train = y[:n_train]
    X_val = X[n_train : n_train + n_val]
    y_val = y[n_train : n_train + n_val]
    X_test = X[n_train + n_val :]
    y_test = y[n_train + n_val :]

    rng = np.random.default_rng(base_seed)
    members = []
    val_preds = []  # n_members x n_val
    test_preds = []  # n_members x n_test

    for i in range(n_members):
        # Bootstrap resample of TRAIN only (n=n_train, with replacement).
        # Val is held out — never enters the bootstrap — so the early-
        # stopping signal d_val inside _train_member is genuinely OOS
        # for every member. Sampling val into the boot would let early
        # stopping overfit on data the model already saw, producing
        # wildly miscalibrated predictions (the 2026-04-26 first run
        # showed AUC 0.50 / ECE_test 0.47 from exactly this leak).
        boot_idx = rng.integers(0, n_train, size=n_train)
        X_boot = X_train[boot_idx]
        y_boot = y_train[boot_idx]

        seed_i = base_seed + i * 17
        log.info(
            "training member %d/%d (seed=%d, boot_unique=%d/%d)",
            i + 1,
            n_members,
            seed_i,
            len(set(boot_idx.tolist())),
            n_train,
        )
        booster, best_params, n_iter = _train_member(
            X_train_val=X_boot,
            y_train_val=y_boot,
            X_val=X_val,
            y_val=y_val,
            optuna_trials=optuna_trials,
            time_budget_s=time_budget_s,
            seed=seed_i,
        )
        members.append((booster, best_params, n_iter))
        val_preds.append(booster.predict(X_val))
        test_preds.append(booster.predict(X_test))

    val_preds = np.asarray(val_preds)
    test_preds = np.asarray(test_preds)

    # Ensemble = mean of member predictions. Stddev = per-prediction
    # cross-member stddev (the quantity we want to use as the Sharpe
    # denominator at serving time).
    val_mean = val_preds.mean(axis=0)
    test_mean = test_preds.mean(axis=0)
    test_std = test_preds.std(axis=0, ddof=1) if n_members > 1 else np.zeros_like(test_mean)

    # Calibrate on val mean with Platt scaling (single sigmoid, 1 DOF).
    # 2026-04-26 ensemble runs with isotonic showed val ECE ≈ 1e-17 and
    # test ECE 0.39 — classic isotonic-on-small-val overfit. Platt has
    # one degree of freedom and cannot memorise.
    calibrator = fit_platt(val_mean, y_val)
    test_calibrated = calibrator.predict(test_mean)
    val_calibrated = calibrator.predict(val_mean)

    ece_val_pre = float(_expected_calibration_error(val_mean, y_val))
    ece_val_post = float(_expected_calibration_error(val_calibrated, y_val))
    ece_test_post = float(_expected_calibration_error(test_calibrated, y_test))

    test_logloss = float(log_loss(y_test, test_calibrated))
    test_auc = float(roc_auc_score(y_test, test_calibrated))
    test_brier = float(brier_score_loss(y_test, test_calibrated))

    test_stddev_summary = {
        "mean": float(test_std.mean()),
        "median": float(np.median(test_std)),
        "p10": float(np.quantile(test_std, 0.10)),
        "p90": float(np.quantile(test_std, 0.90)),
        "min": float(test_std.min()),
        "max": float(test_std.max()),
    }

    per_member_test_auc = [float(roc_auc_score(y_test, p)) for p in test_preds]

    return {
        "members": members,
        "calibrator": calibrator,
        "metrics": {
            "n_members": n_members,
            "test_logloss": test_logloss,
            "test_auc": test_auc,
            "test_brier": test_brier,
            # NB ``ece_val`` keeps the same key as single-model output so
            # ``_passes_promotion`` works without changes; the value here
            # is post-calibration ECE on val.
            "ece_val": ece_val_post,
            "ece_val_pre_calibration": ece_val_pre,
            "ece_test_post_calibration": ece_test_post,
            "calibrated": True,
            "n_train": n_train,
            "n_val": n_val,
            "n_test": len(X_test),
            "per_member_test_auc": per_member_test_auc,
            "best_params_first_member": members[0][1],
        },
        "test_stddev_summary": test_stddev_summary,
    }


def write_artefacts(
    *,
    name: str,
    trained: dict,
    training_period_from: datetime,
    training_period_to: datetime,
    promote: bool,
) -> dict:
    import asyncio

    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    version = f"bb_ofi_ens_{stamp}"
    out_dir = Path("models") / name / version
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, (booster, _params, _n_iter) in enumerate(trained["members"]):
        member_dir = out_dir / f"member_{i:02d}"
        member_dir.mkdir(exist_ok=True)
        booster.save_model(str(member_dir / "model.lgb"))
    with open(out_dir / "calibrator.pkl", "wb") as f:
        pickle.dump(trained["calibrator"], f)

    git_sha = _git_sha()
    meta = {
        "name": name,
        "version": version,
        "kind": "ensemble",
        "n_members": len(trained["members"]),
        "feature_names": list(FEATURE_NAMES),
        "metrics": trained["metrics"],
        "test_stddev_summary": trained["test_stddev_summary"],
        "training_period_from": training_period_from.isoformat(),
        "training_period_to": training_period_to.isoformat(),
        "git_sha": git_sha,
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
                json.dumps(trained["metrics"].get("best_params_first_member", {})),
                training_period_from,
                training_period_to,
                git_sha,
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
        "test_stddev_summary": trained["test_stddev_summary"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="trading.cli.train_bb_ofi_ensemble")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument(
        "--polybot-btc5m",
        default="/btc-tendencia-data/polybot-agent.db",
    )
    ap.add_argument(
        "--polybot-agent",
        default="/btc-tendencia-data/polybot-agent.db",
    )
    ap.add_argument("--slug-encodes-open-ts", action="store_true", default=True)
    ap.add_argument(
        "--slug-encodes-close-ts",
        dest="slug_encodes_open_ts",
        action="store_false",
    )
    ap.add_argument("--members", type=int, default=7)
    ap.add_argument("--optuna-trials", type=int, default=40)
    ap.add_argument("--time-budget-s", type=int, default=600)
    ap.add_argument("--microstructure-window-s", type=int, default=90)
    ap.add_argument("--large-trade-threshold-usd", type=float, default=100_000.0)
    ap.add_argument("--use-real-implied-prob", action="store_true", default=True)
    ap.add_argument(
        "--no-real-implied-prob",
        dest="use_real_implied_prob",
        action="store_false",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--model-name", default="bb_residual_ofi_v1")
    ap.add_argument(
        "--spots-source",
        choices=["polybot", "crypto_trades", "auto"],
        default="crypto_trades",
    )
    ap.add_argument(
        "--markets-source",
        choices=["sqlite", "postgres"],
        default="postgres",
    )
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)
    pg = _pg_dsn()
    log.info(
        "train_bb_ofi_ensemble — period=%s..%s members=%d base_seed=%d model=%s",
        args.date_from,
        args.date_to,
        args.members,
        args.seed,
        args.model_name,
    )

    from trading.engine.data_loader import warn_if_polybot_stale

    for src in (args.polybot_btc5m, args.polybot_agent):
        if Path(src).exists():
            warn_if_polybot_stale(src, expected_window_end_ts=t_to.timestamp())

    tmp_dir = Path(tempfile.mkdtemp(prefix="tea_train_bb_ofi_ens_"))

    def _snapshot(src: str) -> Path | None:
        p = Path(src)
        if not p.exists():
            return None
        dst = tmp_dir / p.name
        shutil.copy2(p, dst)
        return dst

    sqlite_sources: list[Path] = []
    for src in (args.polybot_btc5m, args.polybot_agent):
        snap = _snapshot(src)
        if snap is not None and snap.exists():
            sqlite_sources.append(snap)
    log.info("snapshotted SQLite sources: %s", sqlite_sources)
    if not sqlite_sources:
        log.error("no SQLite sources found; aborting")
        return 2

    markets: list[dict] = []
    if args.markets_source == "postgres":
        markets = _load_settled_markets_from_pg(pg, t_from, t_to)
    else:
        for src in sqlite_sources:
            markets.extend(
                _load_resolved_markets(
                    src,
                    slug_encodes_open_ts=args.slug_encodes_open_ts,
                    pg_dsn=pg,
                )
            )
    seen: set[str] = set()
    markets_in_range: list[dict] = []
    for m in markets:
        if m["slug"] in seen:
            continue
        if not (t_from.timestamp() <= m["close_ts"] <= t_to.timestamp()):
            continue
        seen.add(m["slug"])
        markets_in_range.append(m)
    log.info("resolved markets in range: %d", len(markets_in_range))
    if not markets_in_range:
        log.error("no markets in range; aborting")
        return 2

    samples = build_samples(
        markets_in_range,
        sqlite_sources=sqlite_sources,
        pg_dsn=pg,
        use_real_implied_prob=args.use_real_implied_prob,
        microstructure_window_s=args.microstructure_window_s,
        large_threshold_usd=args.large_trade_threshold_usd,
        spots_source=args.spots_source,
    )
    if len(samples) < 50:
        log.error("too few samples (%d) — need ≥ 50", len(samples))
        return 2

    log.info(
        "training ensemble — n=%d members=%d trials/member=%d budget/member_s=%d",
        len(samples),
        args.members,
        args.optuna_trials,
        args.time_budget_s,
    )
    trained = _train_ensemble(
        samples,
        n_members=args.members,
        optuna_trials=args.optuna_trials,
        time_budget_s=args.time_budget_s,
        base_seed=args.seed,
    )
    log.info("ensemble metrics: %s", trained["metrics"])
    log.info("test stddev summary: %s", trained["test_stddev_summary"])

    result = write_artefacts(
        name=args.model_name,
        trained=trained,
        training_period_from=t_from,
        training_period_to=t_to,
        promote=args.promote,
    )
    log.info(
        "artefacts written — version=%s path=%s passes_gate=%s is_active=%s",
        result["version"],
        result["path"],
        result["passes_gate"],
        result["is_active"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
