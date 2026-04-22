"""Walk-forward runner.

Rolling splits: train window N days, test window M days, step K days.
For each split runs run_backtest on the test window only; train window is
reserved for parameter fitting in later phases (Phase 2 uses fixed params).

Verdict: `stable` if both win-rate and total PnL on OOS fall within ±30%
of the mean across splits; otherwise `unstable`. The tolerance mirrors
the design doc Fase 2 criterion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from trading.engine.backtest_driver import (
    BacktestRunResult,
    EntryWindowConfig,
    FillConfig,
    IndicatorConfig,
    compute_kpis,
    run_backtest,
)
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.risk import RiskManager
from trading.engine.strategy_base import StrategyBase


@dataclass
class WalkForwardSplit:
    fold: int
    train_from: float
    train_to: float
    test_from: float
    test_to: float
    n_trades: int
    kpis: dict


@dataclass
class WalkForwardResult:
    strategy: str
    splits: list[WalkForwardSplit]
    verdict: str
    summary: dict


def _step(ts: datetime, days: int) -> datetime:
    return ts + timedelta(days=days)


def run_walk_forward(
    strategy_factory,
    loader: PolybotSQLiteLoader,
    from_dt: datetime,
    to_dt: datetime,
    train_days: int,
    test_days: int,
    step_days: int,
    stake_usd: float,
    fill_cfg: FillConfig,
    entry_window: EntryWindowConfig,
    risk_cfg: dict,
    indicator_cfg: IndicatorConfig,
    config_used: dict,
    seed: int = 42,
    tolerance: float = 0.30,
) -> WalkForwardResult:
    splits: list[WalkForwardSplit] = []
    cursor = from_dt
    fold = 0
    while True:
        train_from = cursor
        train_to = _step(train_from, train_days)
        test_from = train_to
        test_to = _step(test_from, test_days)
        if test_to > to_dt:
            break

        strategy: StrategyBase = strategy_factory()
        result: BacktestRunResult = run_backtest(
            strategy=strategy,
            loader=loader,
            from_ts=test_from.timestamp(),
            to_ts=test_to.timestamp(),
            stake_usd=stake_usd,
            fill_cfg=fill_cfg,
            entry_window=entry_window,
            risk_manager=RiskManager({"risk": risk_cfg}),
            config_used=config_used,
            indicator_cfg=indicator_cfg,
            seed=seed,
        )
        kpis = compute_kpis(result.trades, (test_to - test_from).total_seconds())
        splits.append(
            WalkForwardSplit(
                fold=fold,
                train_from=train_from.timestamp(),
                train_to=train_to.timestamp(),
                test_from=test_from.timestamp(),
                test_to=test_to.timestamp(),
                n_trades=result.n_trades,
                kpis=kpis,
            )
        )
        cursor = _step(cursor, step_days)
        fold += 1

    if not splits:
        return WalkForwardResult(
            strategy="n/a",
            splits=[],
            verdict="insufficient_data",
            summary={"reason": "window < train_days + test_days"},
        )

    # Summary: mean + relative drift of total_pnl and win_rate.
    pnls = [s.kpis["performance"]["total_pnl"] for s in splits]
    wrs = [s.kpis["performance"]["win_rate"] for s in splits]
    mean_pnl = sum(pnls) / len(pnls)
    mean_wr = sum(wrs) / len(wrs)

    def _in_band(values: list[float], mean: float) -> bool:
        if mean == 0.0:
            return all(abs(v) < 1e-6 for v in values)
        return all(abs(v - mean) / abs(mean) <= tolerance for v in values)

    stable = _in_band(pnls, mean_pnl) and _in_band(wrs, mean_wr)
    summary = {
        "n_splits": len(splits),
        "pnl_mean": mean_pnl,
        "pnl_values": pnls,
        "win_rate_mean": mean_wr,
        "win_rate_values": wrs,
        "tolerance": tolerance,
    }
    return WalkForwardResult(
        strategy=splits[0].kpis.get("strategy", "n/a") if splits else "n/a",
        splits=splits,
        verdict="stable" if stable else "unstable",
        summary=summary,
    )


def walk_forward_to_dict(r: WalkForwardResult) -> dict:
    return {
        "strategy": r.strategy,
        "verdict": r.verdict,
        "summary": r.summary,
        "splits": [asdict(s) for s in r.splits],
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
