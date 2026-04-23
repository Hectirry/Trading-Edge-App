"""Isotonic-conformal abstention gate (ADR 0012).

Given a raw model probability ``p_raw`` for the binary event
"close > open", apply an optional isotonic calibrator and decide:

- ``predict_up``    — calibrated ``p > 0.5 + α/2``
- ``predict_down``  — calibrated ``p < 0.5 - α/2``
- ``abstain``       — otherwise

``α=0.25`` gives a symmetric abstention band around 0.5 of width
0.25, i.e. predict only when the calibrated probability crosses
``p < 0.375`` or ``p > 0.625``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Decision = Literal["predict_up", "predict_down", "abstain"]


class CalibratorLike(Protocol):
    def predict(self, x) -> object: ...


@dataclass
class IsotonicConformal:
    alpha: float = 0.25
    calibrator: CalibratorLike | None = None

    def calibrated(self, p_raw: float) -> float:
        if self.calibrator is None:
            return max(0.0, min(1.0, float(p_raw)))
        try:
            out = self.calibrator.predict([float(p_raw)])
            p = float(out[0])
        except Exception:
            p = float(p_raw)
        return max(0.0, min(1.0, p))

    def decide(self, p_raw: float) -> tuple[Decision, float]:
        p = self.calibrated(p_raw)
        lo = 0.5 - self.alpha / 2
        hi = 0.5 + self.alpha / 2
        if p > hi:
            return "predict_up", p
        if p < lo:
            return "predict_down", p
        return "abstain", p
