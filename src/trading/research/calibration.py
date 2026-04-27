"""Probability calibrators with a unified ``predict([p])`` interface so
they swap in for ``IsotonicRegression`` at the runner layer
(``_lgb_runner.LGBRunner``) without touching the inference code.

Why Platt is offered as an alternative to isotonic: in the 2026-04-26
bb_ofi ensemble runs (strategy since discarded) isotonic memorised
val (ECE val ≈ 1e-17) and exploded on test (ECE 0.39). One degree of
freedom (sigmoid) is strictly less overfit-prone than piecewise-constant
isotonic, especially on the small val sets walk-forward folds yield.
"""

from __future__ import annotations


class PlattCalibrator:
    """Single-feature logistic regression wrapper.

    Mirrors ``sklearn.isotonic.IsotonicRegression``'s ``.predict(p)``
    return shape so ``LGBRunner`` can load either kind of pickle.
    """

    def __init__(self, lr) -> None:
        self._lr = lr

    def predict(self, probs):
        import numpy as np

        x = np.asarray(probs, dtype=np.float64).reshape(-1, 1)
        return self._lr.predict_proba(x)[:, 1]


def fit_platt(val_probs, y_val) -> PlattCalibrator:
    """Fit Platt scaling on raw model probabilities. Used by
    ``train_last90s.train`` when the caller passes
    ``calibration='platt'``."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.asarray(val_probs, dtype=np.float64).reshape(-1, 1)
    y = np.asarray(y_val, dtype=np.int32)
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
    lr.fit(x, y)
    return PlattCalibrator(lr)


__all__ = ["PlattCalibrator", "fit_platt"]
