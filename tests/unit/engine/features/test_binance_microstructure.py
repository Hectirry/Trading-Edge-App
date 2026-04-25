"""Unit tests for the 5 Binance microstructure features."""

from __future__ import annotations

import asyncio

import pytest

from trading.engine.features.binance_microstructure import (
    Trade,
    binance_microstructure_features,
    binance_microstructure_from_trades,
    cvd_normalized,
    large_trade_flag,
    signed_trade_autocorr_lag1,
    taker_buy_ratio,
    trade_intensity,
)


def _t(side: str, qty: float = 1.0, price: float = 70_000.0) -> Trade:
    return Trade(price=price, qty=qty, side=side)


# ---------------- cvd_normalized ---------------- #


def test_cvd_all_buys_returns_one() -> None:
    assert cvd_normalized([_t("buy"), _t("buy"), _t("buy")]) == pytest.approx(1.0)


def test_cvd_all_sells_returns_minus_one() -> None:
    assert cvd_normalized([_t("sell"), _t("sell")]) == pytest.approx(-1.0)


def test_cvd_empty_returns_zero() -> None:
    assert cvd_normalized([]) == 0.0


def test_cvd_balanced_volume_returns_zero() -> None:
    trades = [_t("buy", qty=1.0), _t("sell", qty=1.0)]
    assert cvd_normalized(trades) == pytest.approx(0.0)


# ---------------- taker_buy_ratio ---------------- #


def test_taker_buy_ratio_empty_returns_half() -> None:
    assert taker_buy_ratio([]) == 0.5


def test_taker_buy_ratio_zero_volume_returns_half() -> None:
    # Defensive: someone fed qty=0 trades.
    assert taker_buy_ratio([_t("buy", qty=0.0), _t("sell", qty=0.0)]) == 0.5


def test_taker_buy_ratio_three_quarters() -> None:
    trades = [_t("buy", qty=3.0), _t("sell", qty=1.0)]
    assert taker_buy_ratio(trades) == pytest.approx(0.75)


# ---------------- trade_intensity ---------------- #


def test_trade_intensity_degenerate_baseline_returns_one() -> None:
    # Baseline below 100 trades/window → 1.0 sentinel.
    assert trade_intensity(n_in_window=50, baseline_trades_24h=10, window_s=90) == 1.0


def test_trade_intensity_proportional_to_baseline() -> None:
    # 24h has 960 windows of 90s. If baseline_24h = 192_000 trades →
    # baseline_per_window = 200. Window has 100 trades → intensity 0.5.
    assert trade_intensity(
        n_in_window=100, baseline_trades_24h=192_000, window_s=90
    ) == pytest.approx(0.5)


def test_trade_intensity_zero_baseline_returns_one() -> None:
    assert trade_intensity(n_in_window=200, baseline_trades_24h=0, window_s=90) == 1.0


# ---------------- large_trade_flag ---------------- #


def test_large_trade_flag_threshold_high_no_match() -> None:
    trades = [_t("buy", qty=0.1, price=70_000.0)]  # notional = $7_000
    assert large_trade_flag(trades, threshold_usd=100_000.0) == 0.0


def test_large_trade_flag_threshold_met() -> None:
    trades = [_t("buy", qty=2.0, price=70_000.0)]  # notional = $140_000
    assert large_trade_flag(trades, threshold_usd=100_000.0) == 1.0


def test_large_trade_flag_empty() -> None:
    assert large_trade_flag([]) == 0.0


# ---------------- signed_trade_autocorr_lag1 ---------------- #


def test_signed_autocorr_alternating_returns_minus_one() -> None:
    sides = ["buy", "sell"] * 10
    trades = [_t(s) for s in sides]
    assert signed_trade_autocorr_lag1(trades) == pytest.approx(-1.0, abs=1e-9)


def test_signed_autocorr_constant_returns_zero() -> None:
    # var=0 → defensive return.
    trades = [_t("buy") for _ in range(15)]
    assert signed_trade_autocorr_lag1(trades) == 0.0


def test_signed_autocorr_too_few_trades_returns_zero() -> None:
    assert signed_trade_autocorr_lag1([_t("buy"), _t("sell")]) == 0.0


def test_signed_autocorr_persistent_returns_positive() -> None:
    # 10 buys then 10 sells → strong positive autocorrelation (each
    # trade overwhelmingly matches the next).
    trades = [_t("buy") for _ in range(10)] + [_t("sell") for _ in range(10)]
    out = signed_trade_autocorr_lag1(trades)
    assert out > 0.5


# ---------------- aggregator: pure ---------------- #


def test_from_trades_empty_window_sentinels() -> None:
    out = binance_microstructure_from_trades([], baseline_trades_24h=192_000)
    assert out == {
        "bm_cvd_normalized": 0.0,
        "bm_taker_buy_ratio": 0.5,
        "bm_trade_intensity": 0.0,  # 0 trades / non-degenerate baseline
        "bm_large_trade_flag": 0.0,
        "bm_signed_autocorr_lag1": 0.0,
    }


def test_from_trades_keys_are_canonical() -> None:
    out = binance_microstructure_from_trades(
        [_t("buy"), _t("sell"), _t("buy")], baseline_trades_24h=192_000
    )
    assert set(out.keys()) == {
        "bm_cvd_normalized",
        "bm_taker_buy_ratio",
        "bm_trade_intensity",
        "bm_large_trade_flag",
        "bm_signed_autocorr_lag1",
    }
    assert all(isinstance(v, float) for v in out.values())


# ---------------- aggregator: async with mock conn ---------------- #


class _FakeConn:
    def __init__(self, rows: list[tuple], baseline_count: int):
        self._rows = rows
        self._baseline = baseline_count

    async def fetch(self, sql: str, *args):
        return list(self._rows)

    async def fetchrow(self, sql: str, *args):
        return (self._baseline,)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_async_aggregator_with_empty_trades_returns_sentinels() -> None:
    conn = _FakeConn(rows=[], baseline_count=0)
    out = _run(binance_microstructure_features(0.0, conn=conn))
    assert out["bm_cvd_normalized"] == 0.0
    assert out["bm_taker_buy_ratio"] == 0.5
    assert out["bm_trade_intensity"] == 1.0  # zero baseline → degenerate sentinel
    assert out["bm_large_trade_flag"] == 0.0
    assert out["bm_signed_autocorr_lag1"] == 0.0


def test_async_aggregator_no_conn_returns_full_sentinels() -> None:
    out = _run(binance_microstructure_features(0.0, conn=None))
    assert out == {
        "bm_cvd_normalized": 0.0,
        "bm_taker_buy_ratio": 0.5,
        "bm_trade_intensity": 1.0,
        "bm_large_trade_flag": 0.0,
        "bm_signed_autocorr_lag1": 0.0,
    }
