import math

import numpy as np

from trading.engine.afml_features import (
    autocorr,
    book_pressure,
    compute_afml_features,
    cusum_active,
    cusum_events,
    frac_diff_series,
    frac_diff_weights,
    microprice,
    returns_autocorr_multi,
    returns_entropy,
    shannon_entropy,
)


def test_frac_diff_weights_head():
    w = frac_diff_weights(0.4, 5)
    assert w.shape == (5,)
    # First weight (after reverse) is 1.0.
    assert math.isclose(w[-1], 1.0)


def test_frac_diff_series_too_short_returns_zero():
    assert frac_diff_series([100.0] * 30, d=0.4, size=60) == 0.0


def test_frac_diff_series_sensitive_to_trend():
    up = [100.0 + i * 0.1 for i in range(80)]
    down = [100.0 - i * 0.1 for i in range(80)]
    assert frac_diff_series(up, d=0.4, size=60) > frac_diff_series(down, d=0.4, size=60)


def test_microprice_midpoint_when_equal_depth():
    assert abs(microprice(0.50, 0.52, 100, 100) - 0.51) < 1e-9


def test_microprice_weighted_toward_ask_when_bid_deeper():
    # Heavier bid side (bid_depth > ask_depth) pushes price TOWARD the ask.
    p = microprice(0.50, 0.52, 800, 200)
    assert p > 0.515


def test_book_pressure_range():
    assert book_pressure(100, 100) == 0.0
    assert book_pressure(500, 0) == 1.0
    assert book_pressure(0, 500) == -1.0


def test_cusum_events_fires_on_threshold_breach():
    events = cusum_events([0.001] * 5 + [0.01] + [0.001] * 3, threshold=0.005)
    assert 5 in events or len(events) >= 1


def test_cusum_active_true_on_last_tick():
    series = [0.001] * 10 + [0.01]
    assert cusum_active(series, threshold=0.005) == 1


def test_autocorr_perfectly_correlated():
    ar = autocorr([1.0, 2.0, 3.0, 4.0, 5.0], lag=1)
    assert ar > 0.95


def test_autocorr_too_short():
    assert autocorr([1.0, 2.0], lag=3) == 0.0


def test_returns_autocorr_multi_keys():
    prices = [100.0 + i * 0.05 for i in range(100)]
    out = returns_autocorr_multi(prices, lags=(1, 5, 15))
    assert set(out.keys()) == {"ar_1", "ar_5", "ar_15"}


def test_compute_afml_features_sane_output():
    prices = [100.0 + 0.1 * np.sin(i / 5) for i in range(120)]
    feats = compute_afml_features(
        prices,
        pm_yes_bid=0.50,
        pm_yes_ask=0.52,
        pm_depth_yes=500,
        pm_depth_no=300,
        frac_d=0.4,
        frac_size=60,
        cusum_threshold=0.0005,
        cusum_lookback=60,
        ar_lags=(1, 5, 15),
    )
    for k in (
        "fracdiff",
        "rets_entropy",
        "microprice",
        "book_pressure",
        "cusum_active",
        "cusum_event_rate",
        "ar_1",
        "ar_5",
        "ar_15",
    ):
        assert k in feats
        assert math.isfinite(feats[k])


def test_shannon_entropy_constant_returns_zero():
    assert shannon_entropy([1.0, 1.0, 1.0, 1.0]) == 0.0


def test_returns_entropy_handles_short():
    assert returns_entropy([100.0, 100.0]) == 0.0
