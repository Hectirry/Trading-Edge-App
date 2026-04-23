"""Walk-forward core (Phase 3.8).

Pure-Python fold slicing + metric aggregation + verdict logic; no I/O.
Callers feed in per-fold metrics (IS + OOS) and get back:

- the fold verdict (``stable`` / ``drift`` / ``unvalidated_small_sample``
  / ``unstable_aucs`` / ``insufficient_folds``)
- a run-level summary (median AUC, stability index, recommendation)

The trainer dispatch (LGB retraining, HMM refit, rules-eval) lives in
``src/trading/cli/walk_forward.py``; keep this module deterministic so
we can unit-test it without touching the training pipeline.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

Verdict = str  # narrow via typing.Literal when we lock the surface


# Verdict thresholds. Override via config if we later need per-strategy tuning.
AUC_STABLE_WINDOW = 0.03       # |IS - OOS| ≤ 0.03 per-fold ⇒ stable
AUC_DRIFT_WINDOW = 0.05        # |IS - OOS| > 0.05 ⇒ drift
MIN_TRADES_OOS = 20            # < 20 OOS trades ⇒ unvalidated
MIN_FOLDS_FOR_VERDICT = 3      # aggregate verdict needs ≥ 3 valid folds


@dataclass(frozen=True)
class FoldWindow:
    idx: int
    is_from: datetime          # in-sample start
    is_to: datetime            # in-sample end (= OOS start)
    oos_from: datetime
    oos_to: datetime

    @property
    def is_days(self) -> int:
        return int((self.is_to - self.is_from).total_seconds() / 86400)

    @property
    def oos_days(self) -> int:
        return int((self.oos_to - self.oos_from).total_seconds() / 86400)


def build_folds(
    *,
    t_from: datetime, t_to: datetime,
    is_days: int, oos_days: int, step_days: int,
) -> list[FoldWindow]:
    """Tile the [t_from, t_to] range with walking folds.

    Each fold has a contiguous IS window of ``is_days`` followed by an
    OOS window of ``oos_days``. Folds advance by ``step_days``. Final
    folds are dropped if they would overrun ``t_to``.
    """
    if is_days <= 0 or oos_days <= 0 or step_days <= 0:
        raise ValueError("is_days, oos_days, step_days must all be > 0")
    if t_to <= t_from:
        raise ValueError("t_to must be after t_from")
    folds: list[FoldWindow] = []
    anchor = t_from
    idx = 0
    while True:
        is_from = anchor
        is_to = is_from + timedelta(days=is_days)
        oos_from = is_to
        oos_to = oos_from + timedelta(days=oos_days)
        if oos_to > t_to:
            break
        folds.append(FoldWindow(
            idx=idx, is_from=is_from, is_to=is_to,
            oos_from=oos_from, oos_to=oos_to,
        ))
        idx += 1
        anchor = anchor + timedelta(days=step_days)
    return folds


def classify_fold(
    *,
    auc_is: float | None,
    auc_oos: float | None,
    n_trades_oos: int,
) -> Verdict:
    """Per-fold verdict.

    - ``unvalidated_small_sample`` — OOS sample too small to trust.
    - ``drift`` — AUC degrades sharply from IS to OOS.
    - ``unstable_aucs`` — AUC moves > AUC_STABLE_WINDOW but ≤ drift
      cutoff; the model is reacting to regime rather than signal.
    - ``stable`` — otherwise.
    - ``no_model_auc`` — rule-based strategies (no AUC available).
    """
    if n_trades_oos < MIN_TRADES_OOS:
        return "unvalidated_small_sample"
    if auc_is is None or auc_oos is None:
        return "no_model_auc"
    delta = abs(float(auc_is) - float(auc_oos))
    if delta > AUC_DRIFT_WINDOW:
        return "drift"
    if delta > AUC_STABLE_WINDOW:
        return "unstable_aucs"
    return "stable"


def aggregate_verdicts(fold_results: list[dict]) -> dict:
    """Summary across folds.

    Returns a dict with: n_folds, median_auc_oos, stability_index
    (fraction classified stable), dominant verdict, promote_recommendation.
    """
    if not fold_results:
        return {
            "n_folds": 0,
            "median_auc_oos": None,
            "stability_index": 0.0,
            "dominant_verdict": "insufficient_folds",
            "promote_recommendation": "hold",
        }

    verdicts = [r.get("verdict", "unknown") for r in fold_results]
    aucs = [
        r["auc_oos"] for r in fold_results
        if r.get("auc_oos") is not None
    ]
    pnls = [r.get("pnl_oos", 0.0) for r in fold_results if r.get("pnl_oos") is not None]

    n_total = len(fold_results)
    n_stable = sum(1 for v in verdicts if v == "stable")
    stability_index = n_stable / n_total if n_total else 0.0
    median_auc_oos = statistics.median(aucs) if aucs else None
    median_pnl_oos = statistics.median(pnls) if pnls else None
    total_pnl_oos = sum(pnls) if pnls else 0.0

    # Dominant verdict: pick the most common; tie-break to worse.
    from collections import Counter

    priority = {
        "drift": 0, "unstable_aucs": 1, "unvalidated_small_sample": 2,
        "no_model_auc": 3, "stable": 4, "unknown": 5,
    }
    counts = Counter(verdicts)
    max_count = max(counts.values())
    tied = [v for v, c in counts.items() if c == max_count]
    dominant = min(tied, key=lambda v: priority.get(v, 9))

    if n_total < MIN_FOLDS_FOR_VERDICT:
        recommendation = "insufficient_folds"
    elif stability_index >= 0.75 and median_auc_oos is not None and median_auc_oos >= 0.55:
        recommendation = "promote"
    elif stability_index >= 0.5 and total_pnl_oos > 0:
        recommendation = "soak_longer"
    else:
        recommendation = "hold"

    return {
        "n_folds": n_total,
        "n_stable": n_stable,
        "stability_index": round(stability_index, 4),
        "median_auc_oos": median_auc_oos,
        "median_pnl_oos": median_pnl_oos,
        "total_pnl_oos": total_pnl_oos,
        "dominant_verdict": dominant,
        "promote_recommendation": recommendation,
    }


def default_folds(t_from: datetime, t_to: datetime) -> list[FoldWindow]:
    """5-day IS / 1-day OOS / step 1-day, the approved Phase 3.8 default."""
    return build_folds(
        t_from=t_from, t_to=t_to,
        is_days=5, oos_days=1, step_days=1,
    )


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
