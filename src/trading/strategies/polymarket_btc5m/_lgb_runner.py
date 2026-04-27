"""Shared LightGBM runner used by last_90s_forecaster_v3. Originally
lived in last_90s_forecaster_v2; lifted out so v2/v1/contest_* could be
deleted without breaking v3.

Hard guard against silent feature-count drift (train/serve mismatch):
``predict_proba`` raises if the input vector length does not match
``model.num_feature()``. Optional isotonic calibrator loaded from a
sibling pickle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ModelRunner(Protocol):
    def predict_proba(self, x: list[float]) -> float: ...


class LGBRunner:
    def __init__(self, model_path: Path, calibrator_path: Path | None = None) -> None:
        import lightgbm as lgb  # lazy import — tests w/o lightgbm still import the module

        self.booster = lgb.Booster(model_file=str(model_path))
        self.n_features = int(self.booster.num_feature())
        self._calibrator = None
        if calibrator_path is not None and calibrator_path.exists():
            import pickle

            with open(calibrator_path, "rb") as f:
                self._calibrator = pickle.load(f)

    def predict_proba(self, x: list[float]) -> float:
        import numpy as np

        if len(x) != self.n_features:
            raise ValueError(
                f"feature vector length {len(x)} does not match "
                f"model.num_feature() {self.n_features} — train and serve "
                f"must use the same FEATURE_NAMES order."
            )
        arr = np.asarray([x], dtype=np.float64)
        p = float(self.booster.predict(arr)[0])
        if self._calibrator is not None:
            p = float(self._calibrator.predict([p])[0])
        return max(0.0, min(1.0, p))


__all__ = ["LGBRunner", "ModelRunner"]
