"""HMM regime helpers (ADR 0012)."""

from __future__ import annotations

import pytest

from trading.engine.features.hmm_regime import (
    NullHMMRegimeDetector,
    build_feature_matrix,
    canonical_label_order,
    yz_volatility,
)


def test_yz_volatility_zero_on_flat() -> None:
    assert yz_volatility([100.0] * 30) == pytest.approx(0.0, abs=1e-9)


def test_yz_volatility_positive_on_noisy() -> None:
    spots = [100.0 + (0.5 if i % 2 else -0.5) for i in range(30)]
    assert yz_volatility(spots, window=20) > 0.0


def test_yz_volatility_short_series_returns_zero() -> None:
    assert yz_volatility([100.0, 101.0], window=20) == 0.0


def test_build_feature_matrix_shape() -> None:
    closes = [100.0 + 0.1 * i for i in range(25)]
    fm = build_feature_matrix(closes)
    assert len(fm) == 24  # one row per return
    for row in fm:
        assert len(row) == 2
        assert isinstance(row[0], float)
        assert isinstance(row[1], float)


def test_build_feature_matrix_skips_non_positive() -> None:
    closes = [100.0, 0.0, 101.0, 102.0]
    fm = build_feature_matrix(closes)
    # first pair dropped because second close is 0, second pair dropped
    # because it starts from 0 → only one surviving (101 → 102)
    assert len(fm) == 1


def test_canonical_label_order_assigns_all_four() -> None:
    means = [
        (0.001, 0.005),   # low vol, mild positive → maybe bull
        (-0.002, 0.006),  # low vol, negative → bear
        (0.0, 0.004),     # lowest vol, neutral → ranging
        (0.0, 0.05),      # highest vol → high_vol
    ]
    labels = canonical_label_order(means)
    assert sorted(labels) == sorted(
        ["trending_bull", "trending_bear", "ranging", "high_vol"]
    )


def test_canonical_label_order_highest_vol_to_high_vol() -> None:
    means = [
        (0.001, 0.01),
        (-0.001, 0.01),
        (0.0, 0.008),
        (0.002, 0.05),   # clearly highest vol
    ]
    labels = canonical_label_order(means)
    assert labels[3] == "high_vol"


@pytest.mark.asyncio
async def test_null_detector_always_returns_none() -> None:
    det = NullHMMRegimeDetector()
    # Predict is sync in the module but "asyncio" marker just exercises
    # the module via the usual fixture plumbing.
    assert det.predict([100.0] * 50) is None
