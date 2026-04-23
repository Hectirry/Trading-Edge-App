"""Isotonic-conformal decision gate (ADR 0012)."""

from __future__ import annotations

from trading.engine.features.conformal import IsotonicConformal


class _ShiftCalibrator:
    """Stub calibrator: adds +0.10 then clamps to [0, 1]."""

    def predict(self, x):
        return [min(1.0, max(0.0, float(x[0]) + 0.10))]


def test_abstain_in_band() -> None:
    g = IsotonicConformal(alpha=0.25)
    dec, p = g.decide(0.5)
    assert dec == "abstain"
    assert p == 0.5


def test_predict_up_above_hi() -> None:
    g = IsotonicConformal(alpha=0.25)
    dec, p = g.decide(0.65)
    assert dec == "predict_up"
    assert p == 0.65


def test_predict_down_below_lo() -> None:
    g = IsotonicConformal(alpha=0.25)
    dec, _ = g.decide(0.30)
    assert dec == "predict_down"


def test_calibrator_shifts_decision() -> None:
    g = IsotonicConformal(alpha=0.25, calibrator=_ShiftCalibrator())
    # raw 0.55 would be abstain (inside [0.375, 0.625]); +0.10 → 0.65 → predict_up
    assert g.decide(0.55)[0] == "predict_up"


def test_input_clamped_out_of_range() -> None:
    g = IsotonicConformal(alpha=0.25)
    assert g.decide(-0.5)[1] == 0.0
    assert g.decide(1.5)[1] == 1.0


def test_tighter_alpha_widens_abstention() -> None:
    wide = IsotonicConformal(alpha=0.30)
    narrow = IsotonicConformal(alpha=0.05)
    # 0.53 falls outside the tight abstention band but inside the wide one.
    assert wide.decide(0.53)[0] == "abstain"
    assert narrow.decide(0.53)[0] == "predict_up"
