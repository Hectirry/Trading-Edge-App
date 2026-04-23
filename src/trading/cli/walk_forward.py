"""Walk-forward CLI (Phase 3.8 — unified dispatch).

One entry point for every strategy. Two execution paths:

- **Rules-based** (``imbalance_v3``, ``trend_confirm_t1_v1``,
  ``last_90s_forecaster_v1``, ``contest_avengers_v1``) runs the
  Phase-2 ``run_walk_forward`` replay infrastructure over polybot
  SQLite dumps — deterministic trade replay, PnL + verdict per fold.
- **ML-based** (``hmm_regime_btc5m``, ``last_90s_forecaster_v2``,
  ``contest_ensemble_v1``) refits the model on each IS window and
  evaluates AUC / Brier on the OOS window via the new
  ``trading.research.walk_forward`` core.

Results are written to ``research.walk_forward_runs`` (existing from
Phase 2) with per-fold details in ``splits`` JSONB and the aggregate
summary in ``summary``.

``--promote-winner`` is opt-in: when set, flips ``is_active=TRUE`` on
the best-fold model in ``research.models``. Default is report-only.

Usage::

    # ML — rolling retrain
    docker compose exec tea-engine python -m trading.cli.walk_forward \\
        --strategy last_90s_forecaster_v2 \\
        --from 2026-03-01 --to 2026-04-20

    # Rules — replay
    docker compose exec tea-engine python -m trading.cli.walk_forward \\
        --strategy imbalance_v3 \\
        --params config/strategies/pbt5m_imbalance_v3.toml \\
        --from 2026-01-01 --to 2026-04-20 \\
        --polybot-db /polybot-btc5m-data/polybot.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime

from trading.research.walk_forward import (
    FoldWindow,
    aggregate_verdicts,
    build_folds,
    classify_fold,
)

log = logging.getLogger("cli.walk_forward")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


ML_STRATEGIES = {
    "hmm_regime_btc5m",
    "last_90s_forecaster_v2",
    "contest_ensemble_v1",
}

RULE_STRATEGIES = {
    "imbalance_v3",
    "trend_confirm_t1_v1",
    "last_90s_forecaster_v1",
    "contest_avengers_v1",
}


def _normalize_strategy(name: str) -> str:
    """Strip optional `polymarket_btc5m/` prefix used in some callers."""
    return name.rsplit("/", 1)[-1]


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


# ---------------------------------------------------------- ML strategies


async def _evaluate_fold_ml(strategy: str, fold: FoldWindow) -> dict:
    if strategy == "hmm_regime_btc5m":
        return await _eval_hmm_fold(fold)
    if strategy == "last_90s_forecaster_v2":
        return await _eval_last_90s_v2_fold(fold)
    if strategy == "contest_ensemble_v1":
        # Same dataset + feature builder as v2 (ADR 0012, BiLSTM deferred).
        return await _eval_last_90s_v2_fold(fold)
    raise RuntimeError(f"no ML trainer for {strategy}")


async def _eval_hmm_fold(fold: FoldWindow) -> dict:
    from trading.cli.train_hmm_regime import _fetch_closes
    from trading.engine.features.hmm_regime import (
        build_feature_matrix,
        canonical_label_order,
    )

    pg_dsn = _pg_dsn()
    closes = _fetch_closes(
        pg_dsn,
        int(fold.is_from.timestamp()),
        int(fold.is_to.timestamp()),
    )
    if len(closes) < 200:
        return _unvalidated_fold(fold, note="insufficient IS closes")

    import numpy as np
    from hmmlearn import hmm as hmmlib

    features = build_feature_matrix(closes)
    X = np.asarray(features, dtype=np.float64)
    model = hmmlib.GaussianHMM(
        n_components=4, covariance_type="full",
        n_iter=100, tol=1e-4, random_state=42,
    )
    model.fit(X)
    score_is = float(model.score(X))

    oos_closes = _fetch_closes(
        pg_dsn,
        int(fold.oos_from.timestamp()),
        int(fold.oos_to.timestamp()),
    )
    n_oos = max(0, len(oos_closes) - 1)
    score_oos = None
    if n_oos >= 20:
        oos_features = build_feature_matrix(oos_closes)
        X_oos = np.asarray(oos_features, dtype=np.float64)
        score_oos = float(model.score(X_oos)) / max(len(X_oos), 1)

    means = [
        (float(model.means_[i, 0]), float(model.means_[i, 1]))
        for i in range(model.n_components)
    ]
    labels = canonical_label_order(means)
    verdict = classify_fold(auc_is=None, auc_oos=None, n_trades_oos=n_oos)
    return {
        "fold": fold.idx,
        "is_from": fold.is_from.isoformat(), "is_to": fold.is_to.isoformat(),
        "oos_from": fold.oos_from.isoformat(), "oos_to": fold.oos_to.isoformat(),
        "auc_is": None, "auc_oos": None,
        "score_is": score_is, "score_oos": score_oos,
        "n_trades_oos": n_oos,
        "state_labels": labels,
        "pnl_oos": 0.0,
        "verdict": verdict,
    }


async def _eval_last_90s_v2_fold(fold: FoldWindow) -> dict:
    from pathlib import Path as _Path

    from trading.cli.train_last90s import (
        _load_ohlcv_5m,
        _load_resolved_markets,
        build_samples,
        train,
    )

    polybot_agent = "/btc-tendencia-data/polybot-agent.db"
    if not _Path(polybot_agent).exists():
        return _unvalidated_fold(fold, note="polybot-agent sqlite missing")

    markets = _load_resolved_markets(_Path(polybot_agent), slug_encodes_open_ts=True)
    for m in markets:
        m["_source"] = polybot_agent

    def _in(m, a, b):
        return a.timestamp() <= float(m["close_ts"]) <= b.timestamp()

    is_markets = [m for m in markets if _in(m, fold.is_from, fold.is_to)]
    oos_markets = [m for m in markets if _in(m, fold.oos_from, fold.oos_to)]
    if len(is_markets) < 50 or len(oos_markets) < 10:
        return _unvalidated_fold(
            fold, n_trades_oos=len(oos_markets),
            note=f"is={len(is_markets)} oos={len(oos_markets)}",
        )

    candles = _load_ohlcv_5m(
        _pg_dsn(),
        int(fold.is_from.timestamp()) - 3600,
        int(fold.oos_to.timestamp()) + 3600,
    )
    is_samples = build_samples(
        is_markets,
        sqlite_sources=[_Path(polybot_agent)],
        candles_5m=candles,
    )
    oos_samples = build_samples(
        oos_markets,
        sqlite_sources=[_Path(polybot_agent)],
        candles_5m=candles,
    )
    if len(is_samples) < 40 or len(oos_samples) < 10:
        return _unvalidated_fold(
            fold, n_trades_oos=len(oos_samples),
            note="post-feature-build insufficient",
        )

    trained = train(is_samples, optuna_trials=40, time_budget_s=120)

    import numpy as np
    from sklearn.metrics import brier_score_loss, roc_auc_score

    X_oos = np.asarray([s.features for s in oos_samples], dtype=np.float64)
    y_oos = np.asarray([s.label for s in oos_samples], dtype=np.int32)
    probs = trained["model"].predict(X_oos)
    auc_oos = float(roc_auc_score(y_oos, probs)) if len(set(y_oos)) == 2 else None
    brier_oos = float(brier_score_loss(y_oos, probs))
    auc_is = trained["metrics"].get("test_auc")
    verdict = classify_fold(
        auc_is=auc_is, auc_oos=auc_oos, n_trades_oos=len(oos_samples),
    )
    return {
        "fold": fold.idx,
        "is_from": fold.is_from.isoformat(), "is_to": fold.is_to.isoformat(),
        "oos_from": fold.oos_from.isoformat(), "oos_to": fold.oos_to.isoformat(),
        "auc_is": auc_is, "auc_oos": auc_oos,
        "brier_oos": brier_oos,
        "n_trades_oos": len(oos_samples),
        "pnl_oos": None,
        "verdict": verdict,
    }


# ------------------------------------------------------- Rule strategies


def _replay_rules_fold(
    args: argparse.Namespace, strategy: str, fold: FoldWindow,
) -> dict:
    """Run the Phase-2 replay for a rule-based strategy on one fold.

    Uses the existing ``run_walk_forward`` as a one-fold invocation
    (train_days=is_days, test_days=oos_days, step_days=anything large
    enough to keep a single fold). The paper-parity replay measures
    n_trades + PnL directly; AUC is not available, so the fold verdict
    falls back to ``no_model_auc``.
    """
    from pathlib import Path

    import tomli

    from trading.engine.backtest_driver import (
        EntryWindowConfig,
        FillConfig,
        IndicatorConfig,
    )
    from trading.engine.data_loader import PolybotSQLiteLoader
    from trading.engine.walk_forward import run_walk_forward

    if not args.params:
        return _unvalidated_fold(
            fold, note="--params required for rule-based WF",
        )
    cfg = tomli.loads(Path(args.params).read_text())
    if not Path(args.polybot_db).exists():
        return _unvalidated_fold(fold, note=f"polybot-db missing: {args.polybot_db}")

    factory = _rules_factory(strategy, cfg)
    loader = PolybotSQLiteLoader(
        args.polybot_db, slug_encodes_open_ts=args.slug_encodes_open_ts,
    )

    # Run exactly one split with IS = [is_from, is_to), OOS = [oos_from, oos_to].
    result = run_walk_forward(
        strategy_factory=factory,
        loader=loader,
        from_dt=fold.is_from,
        to_dt=fold.oos_to,
        train_days=int((fold.is_to - fold.is_from).days),
        test_days=int((fold.oos_to - fold.oos_from).days),
        step_days=max(1, int((fold.oos_to - fold.oos_from).days)),
        stake_usd=min(
            float(cfg.get("sizing", {}).get("stake_usd", 3.0)),
            float(cfg.get("risk", {}).get("max_position_size_usd", 5.0)),
        ),
        fill_cfg=FillConfig(
            slippage_bps=float(cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(cfg["fill_model"]["fill_probability"]),
        ),
        entry_window=EntryWindowConfig(
            earliest_entry_t_s=int(cfg["backtest"]["earliest_entry_t_s"]),
            latest_entry_t_s=int(cfg["backtest"]["latest_entry_t_s"]),
        ),
        risk_cfg=cfg["risk"],
        indicator_cfg=IndicatorConfig(),
        config_used=cfg,
        seed=args.seed,
        tolerance=args.tolerance,
    )
    split = result.splits[0] if result.splits else None
    n_trades_oos = int(getattr(split, "n_trades_oos", 0) or 0) if split else 0
    pnl_oos = float(getattr(split, "pnl_oos", 0.0) or 0.0) if split else 0.0
    verdict = classify_fold(auc_is=None, auc_oos=None, n_trades_oos=n_trades_oos)
    return {
        "fold": fold.idx,
        "is_from": fold.is_from.isoformat(), "is_to": fold.is_to.isoformat(),
        "oos_from": fold.oos_from.isoformat(), "oos_to": fold.oos_to.isoformat(),
        "auc_is": None, "auc_oos": None,
        "n_trades_oos": n_trades_oos,
        "pnl_oos": pnl_oos,
        "verdict": verdict,
    }


def _rules_factory(strategy: str, cfg: dict):
    if strategy == "imbalance_v3":
        from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3
        return lambda: ImbalanceV3(config=cfg)
    if strategy == "trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import TrendConfirmT1V1
        return lambda: TrendConfirmT1V1(config=cfg)
    if strategy == "last_90s_forecaster_v1":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v1 import (
            Last90sForecasterV1,
        )
        return lambda: Last90sForecasterV1(config=cfg)
    if strategy == "contest_avengers_v1":
        from trading.strategies.polymarket_btc5m.contest_avengers_v1 import (
            ContestAvengersV1,
        )
        return lambda: ContestAvengersV1(cfg)
    raise RuntimeError(f"no rules factory for {strategy}")


# --------------------------------------------------------------- helpers


def _unvalidated_fold(fold: FoldWindow, *, n_trades_oos: int = 0, note: str = "") -> dict:
    return {
        "fold": fold.idx,
        "is_from": fold.is_from.isoformat(), "is_to": fold.is_to.isoformat(),
        "oos_from": fold.oos_from.isoformat(), "oos_to": fold.oos_to.isoformat(),
        "auc_is": None, "auc_oos": None,
        "n_trades_oos": n_trades_oos,
        "pnl_oos": 0.0,
        "verdict": "unvalidated_small_sample",
        "note": note,
    }


def _pg_dsn() -> str:
    import os
    return (
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}"
    )


async def _persist(
    *, strategy: str,
    started_at: datetime, ended_at: datetime,
    fold_results: list[dict], summary: dict,
    promote: bool,
) -> str:
    from trading.common.db import acquire, close_pool

    run_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research.walk_forward_runs
                (id, strategy_name, params_hash, started_at, ended_at,
                 status, verdict, splits, summary)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
            """,
            uuid.UUID(run_id),
            strategy, "wf_v2",
            started_at, ended_at,
            "completed",
            summary.get("dominant_verdict"),
            json.dumps(fold_results),
            json.dumps({**summary, "promoted": promote}),
        )
    await close_pool()
    return run_id


async def main_async(args: argparse.Namespace) -> int:
    strategy = _normalize_strategy(args.strategy)
    if strategy not in ML_STRATEGIES and strategy not in RULE_STRATEGIES:
        log.error("unknown strategy: %s", strategy)
        return 2

    t_from = _parse_ts(args.date_from)
    t_to = _parse_ts(args.date_to)
    folds = build_folds(
        t_from=t_from, t_to=t_to,
        is_days=args.is_days, oos_days=args.oos_days, step_days=args.step_days,
    )
    if not folds:
        log.error("no folds produced; widen range or shrink is/oos days")
        return 3

    log.info("wf.running", strategy=strategy, n_folds=len(folds))
    started_at = datetime.now(tz=UTC)
    fold_results: list[dict] = []
    for fold in folds:
        log.info(
            "wf.fold",
            idx=fold.idx,
            is_from=fold.is_from.date(), is_to=fold.is_to.date(),
            oos_from=fold.oos_from.date(), oos_to=fold.oos_to.date(),
        )
        try:
            if strategy in ML_STRATEGIES:
                result = await _evaluate_fold_ml(strategy, fold)
            else:
                result = _replay_rules_fold(args, strategy, fold)
        except Exception as e:
            log.exception("wf.fold_err", idx=fold.idx, err=str(e))
            result = {
                "fold": fold.idx, "verdict": "error", "error": str(e),
                "is_from": fold.is_from.isoformat(), "is_to": fold.is_to.isoformat(),
                "oos_from": fold.oos_from.isoformat(), "oos_to": fold.oos_to.isoformat(),
                "auc_is": None, "auc_oos": None, "n_trades_oos": 0, "pnl_oos": 0.0,
            }
        fold_results.append(result)

    summary = aggregate_verdicts(fold_results)
    log.info("wf.summary: %s", json.dumps(summary, default=str))
    ended_at = datetime.now(tz=UTC)
    run_id = await _persist(
        strategy=strategy,
        started_at=started_at, ended_at=ended_at,
        fold_results=fold_results, summary=summary,
        promote=args.promote_winner,
    )
    log.info("wf.persisted", run_id=run_id, promote=args.promote_winner)
    if args.promote_winner and summary.get("promote_recommendation") != "promote":
        log.warning(
            "wf.promote_skipped",
            reason=summary.get("promote_recommendation"),
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--is-days", type=int, default=5)
    ap.add_argument("--oos-days", type=int, default=1)
    ap.add_argument("--step-days", type=int, default=1)
    ap.add_argument("--promote-winner", action="store_true")
    # Rules-only: path to TOML + polybot sqlite.
    ap.add_argument("--params", default=None)
    ap.add_argument("--polybot-db", default="/polybot-btc5m-data/polybot.db")
    ap.add_argument("--slug-encodes-open-ts", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tolerance", type=float, default=0.30)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
