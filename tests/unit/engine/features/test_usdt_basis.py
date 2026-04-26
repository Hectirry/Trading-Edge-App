"""USDT basis helpers — pure-function tests."""

from __future__ import annotations

import pytest

from trading.engine.features.usdt_basis import (
    BASIS_MAX,
    BASIS_MIN,
    MAX_STALENESS_S,
    basis_at,
    implicit_basis,
)


def test_implicit_basis_typical_drift() -> None:
    # Coinbase $67,500 / Binance $67,540 → USDT priced at 0.99941 USD.
    b = implicit_basis(binance_btcusdt_close=67_540.0, coinbase_btcusd_close=67_500.0)
    assert b is not None
    assert 0.999 < b < 1.000


def test_implicit_basis_rejects_bad_inputs() -> None:
    assert implicit_basis(None, 67_500.0) is None
    assert implicit_basis(67_540.0, None) is None
    assert implicit_basis(0.0, 67_500.0) is None
    assert implicit_basis(67_540.0, -1.0) is None


def test_implicit_basis_rejects_out_of_bounds() -> None:
    # +10 % USDT premium is unrealistic — drop.
    assert implicit_basis(binance_btcusdt_close=60_000.0, coinbase_btcusd_close=66_000.0) is None
    # -10 % depeg — drop.
    assert implicit_basis(binance_btcusdt_close=66_000.0, coinbase_btcusd_close=60_000.0) is None


def test_basis_at_returns_most_recent() -> None:
    series = [(1_700_000_000.0, 0.9995), (1_700_000_060.0, 0.9998), (1_700_000_120.0, 1.0001)]
    # Lookup at exactly the second timestamp.
    assert basis_at(1_700_000_060.0, series) == pytest.approx(0.9998)
    # Lookup between observations — returns the latest one ≤ ts.
    assert basis_at(1_700_000_059.0, series) == pytest.approx(0.9995)
    assert basis_at(1_700_000_119.0, series) == pytest.approx(0.9998)
    # Lookup after the last observation, within staleness window.
    assert basis_at(1_700_000_300.0, series) == pytest.approx(1.0001)


def test_basis_at_stale_falls_back_to_one() -> None:
    series = [(1_700_000_000.0, 0.9995)]
    # Observation is older than MAX_STALENESS_S — sentinel.
    too_late = 1_700_000_000.0 + MAX_STALENESS_S + 1
    assert basis_at(too_late, series) == 1.0


def test_basis_at_no_observations_returns_one() -> None:
    assert basis_at(1_700_000_000.0, []) == 1.0


def test_basis_at_before_first_obs_returns_one() -> None:
    series = [(1_700_000_100.0, 0.9995)]
    # Lookup BEFORE the first observation: no info → sentinel.
    assert basis_at(1_700_000_000.0, series) == 1.0


def test_basis_constants_sane() -> None:
    assert BASIS_MIN < 1.0 < BASIS_MAX
    assert MAX_STALENESS_S > 60
