"""Probability calibrators with a unified ``predict([p])`` interface so
they swap in for ``IsotonicRegression`` at the runner layer
(``_lgb_runner.LGBRunner``) without touching the inference code.

Why Platt instead of isotonic for bb_residual_ofi_v1: the 2026-04-26
ensemble runs showed isotonic memorising val (ECE val ≈ 1e-17) and
exploding on test (ECE 0.39). One degree of freedom (sigmoid) is
strictly less overfit-prone than the piecewise-constant fit of
isotonic, especially on the small val sets we get inside walk-forward
folds.
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
    ``train_last90s.train`` and ``train_bb_ofi_ensemble`` when the
    caller passes ``calibration='platt'``."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.asarray(val_probs, dtype=np.float64).reshape(-1, 1)
    y = np.asarray(y_val, dtype=np.int32)
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
    lr.fit(x, y)
    return PlattCalibrator(lr)


__all__ = ["PlattCalibrator", "fit_platt"]
