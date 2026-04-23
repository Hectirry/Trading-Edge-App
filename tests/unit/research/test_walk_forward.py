"""Walk-forward core (Phase 3.8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.research.walk_forward import (
    aggregate_verdicts,
    build_folds,
    classify_fold,
    default_folds,
)


def _dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_build_folds_simple() -> None:
    folds = build_folds(
        t_from=_dt(2026, 1, 1), t_to=_dt(2026, 1, 10),
        is_days=5, oos_days=1, step_days=1,
    )
    # 5d IS + 1d OOS → folds end at 6, 7, 8, 9, 10 → 5 folds
    assert len(folds) == 4  # last fold OOS ends on Jan 10 (inclusive upper bound)
    assert folds[0].is_days == 5
    assert folds[0].oos_days == 1
    # each fold advances 1 day
    for a, b in zip(folds[:-1], folds[1:], strict=True):
        assert (b.is_from - a.is_from).days == 1


def test_build_folds_drops_overrun() -> None:
    folds = build_folds(
        t_from=_dt(2026, 1, 1), t_to=_dt(2026, 1, 7),
        is_days=5, oos_days=2, step_days=1,
    )
    # Fold 0: IS 1-6, OOS 6-8. 8 > 7 → dropped. No folds.
    assert folds == []


def test_build_folds_rejects_bad_range() -> None:
    with pytest.raises(ValueError):
        build_folds(
            t_from=_dt(2026, 1, 10), t_to=_dt(2026, 1, 1),
            is_days=5, oos_days=1, step_days=1,
        )


def test_build_folds_rejects_non_positive_days() -> None:
    with pytest.raises(ValueError):
        build_folds(
            t_from=_dt(2026, 1, 1), t_to=_dt(2026, 1, 10),
            is_days=0, oos_days=1, step_days=1,
        )


def test_classify_fold_unvalidated_small_sample() -> None:
    assert classify_fold(auc_is=0.60, auc_oos=0.60, n_trades_oos=5) \
        == "unvalidated_small_sample"


def test_classify_fold_stable() -> None:
    assert classify_fold(auc_is=0.60, auc_oos=0.61, n_trades_oos=50) == "stable"


def test_classify_fold_unstable() -> None:
    assert classify_fold(auc_is=0.60, auc_oos=0.56, n_trades_oos=50) \
        == "unstable_aucs"


def test_classify_fold_drift() -> None:
    assert classify_fold(auc_is=0.62, auc_oos=0.52, n_trades_oos=50) == "drift"


def test_classify_fold_no_auc_for_rules_strategy() -> None:
    # Rule-based strategies report n_trades but no AUC.
    assert classify_fold(auc_is=None, auc_oos=None, n_trades_oos=50) \
        == "no_model_auc"


def test_aggregate_verdicts_empty_returns_hold() -> None:
    summary = aggregate_verdicts([])
    assert summary["n_folds"] == 0
    assert summary["promote_recommendation"] == "hold"
    assert summary["dominant_verdict"] == "insufficient_folds"


def test_aggregate_verdicts_promotes_stable_high_auc() -> None:
    folds = [
        {"verdict": "stable", "auc_oos": 0.62, "pnl_oos": 10.0},
        {"verdict": "stable", "auc_oos": 0.60, "pnl_oos": 8.0},
        {"verdict": "stable", "auc_oos": 0.58, "pnl_oos": 5.0},
        {"verdict": "stable", "auc_oos": 0.57, "pnl_oos": 3.0},
    ]
    summary = aggregate_verdicts(folds)
    assert summary["n_folds"] == 4
    assert summary["stability_index"] == 1.0
    assert summary["promote_recommendation"] == "promote"
    assert summary["dominant_verdict"] == "stable"


def test_aggregate_verdicts_holds_on_drift() -> None:
    folds = [
        {"verdict": "drift", "auc_oos": 0.50, "pnl_oos": -3.0},
        {"verdict": "drift", "auc_oos": 0.48, "pnl_oos": -5.0},
        {"verdict": "drift", "auc_oos": 0.52, "pnl_oos": 1.0},
    ]
    summary = aggregate_verdicts(folds)
    assert summary["dominant_verdict"] == "drift"
    assert summary["promote_recommendation"] == "hold"


def test_aggregate_verdicts_soak_longer_when_mid_stability_positive_pnl() -> None:
    # 2/4 = 0.5 stability, positive PnL, median AUC below 0.55 → soak_longer.
    folds = [
        {"verdict": "stable", "auc_oos": 0.54, "pnl_oos": 5.0},
        {"verdict": "stable", "auc_oos": 0.53, "pnl_oos": 3.0},
        {"verdict": "unstable_aucs", "auc_oos": 0.52, "pnl_oos": 2.0},
        {"verdict": "drift", "auc_oos": 0.50, "pnl_oos": -1.0},
    ]
    summary = aggregate_verdicts(folds)
    assert summary["promote_recommendation"] == "soak_longer"


def test_aggregate_verdicts_insufficient_folds() -> None:
    folds = [{"verdict": "stable", "auc_oos": 0.62, "pnl_oos": 5.0}]
    summary = aggregate_verdicts(folds)
    assert summary["promote_recommendation"] == "insufficient_folds"


def test_default_folds_phase_38_shape() -> None:
    folds = default_folds(_dt(2026, 1, 1), _dt(2026, 1, 11))
    # 5d IS + 1d OOS, step 1d → folds cover [1..6, 6..7], [2..7, 7..8], etc
    # Last fold must end ≤ Jan 11.
    assert all(f.is_days == 5 and f.oos_days == 1 for f in folds)
    assert len(folds) >= 5
    for f in folds:
        assert f.oos_to <= _dt(2026, 1, 11)
