"""AFML features — literal port of /home/coder/BTC-Tendencia-5m/core/afml_features.py.

Frac-diff, autocorr, CUSUM, microprice, and the unified dispatcher
`compute_afml_features` that `trend_confirm_t1_v1` calls each tick.
Same behavior, same numpy dependency, same sanitize pass at the end.
"""

from __future__ import annotations

import math

import numpy as np


# ---------------------------------------------------------------------------
# Fractional differentiation
# ---------------------------------------------------------------------------
def frac_diff_weights(d: float, size: int) -> np.ndarray:
    w = [1.0]
    for k in range(1, size):
        w_k = -w[-1] * (d - k + 1) / k
        w.append(w_k)
    arr = np.array(w, dtype=np.float64)
    return arr[::-1]


def frac_diff_series(series, d: float = 0.4, size: int = 60) -> float:
    arr = np.asarray(series, dtype=np.float64)
    if arr.size < size:
        return 0.0
    w = frac_diff_weights(d, size)
    window = arr[-size:]
    return float(np.dot(w, window))


# ---------------------------------------------------------------------------
# Shannon entropy + returns entropy
# ---------------------------------------------------------------------------
def shannon_entropy(series, bins: int = 10) -> float:
    arr = np.asarray(series, dtype=np.float64)
    if arr.size < 2:
        return 0.0
    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1e-12:
        return 0.0
    hist, _ = np.histogram(arr, bins=bins, range=(lo, hi))
    total = hist.sum()
    if total == 0:
        return 0.0
    p = hist / total
    p = p[p > 0]
    return float(-np.sum(p * np.log(p)))


def returns_entropy(prices, bins: int = 10) -> float:
    arr = np.asarray(prices, dtype=np.float64)
    if arr.size < 3:
        return 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(arr))
    rets = rets[np.isfinite(rets)]
    return shannon_entropy(rets, bins=bins)


# ---------------------------------------------------------------------------
# Microprice / book pressure
# ---------------------------------------------------------------------------
def microprice(bid: float, ask: float, bid_depth: float, ask_depth: float) -> float:
    total = bid_depth + ask_depth
    if total <= 0 or bid <= 0 or ask <= 0:
        return (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
    return ask * (bid_depth / total) + bid * (ask_depth / total)


def book_pressure(bid_depth: float, ask_depth: float) -> float:
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


# ---------------------------------------------------------------------------
# CUSUM
# ---------------------------------------------------------------------------
def cusum_events(series, threshold: float) -> list[int]:
    arr = np.asarray(series, dtype=np.float64)
    pos = 0.0
    neg = 0.0
    events: list[int] = []
    for i, x in enumerate(arr):
        pos = max(0.0, pos + x)
        neg = min(0.0, neg + x)
        if pos > threshold:
            events.append(i)
            pos = 0.0
        elif neg < -threshold:
            events.append(i)
            neg = 0.0
    return events


def cusum_active(series, threshold: float) -> int:
    events = cusum_events(series, threshold)
    if not events:
        return 0
    return 1 if events[-1] == len(np.asarray(series)) - 1 else 0


# ---------------------------------------------------------------------------
# Autocorrelation
# ---------------------------------------------------------------------------
def autocorr(series, lag: int = 1) -> float:
    arr = np.asarray(series, dtype=np.float64)
    if arr.size <= lag + 1:
        return 0.0
    a = arr[:-lag]
    b = arr[lag:]
    va = a - a.mean()
    vb = b - b.mean()
    denom = math.sqrt((va**2).sum() * (vb**2).sum())
    if denom <= 0:
        return 0.0
    return float((va * vb).sum() / denom)


def returns_autocorr_multi(prices, lags: tuple[int, ...] = (1, 5, 15)) -> dict:
    arr = np.asarray(prices, dtype=np.float64)
    if arr.size < max(lags) + 2:
        return {f"ar_{lag}": 0.0 for lag in lags}
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(arr))
    rets = rets[np.isfinite(rets)]
    return {f"ar_{lag}": autocorr(rets, lag) for lag in lags}


# ---------------------------------------------------------------------------
# Unified feature builder
# ---------------------------------------------------------------------------
def compute_afml_features(
    spot_prices,
    *,
    pm_yes_bid: float = 0.0,
    pm_yes_ask: float = 0.0,
    pm_depth_yes: float = 0.0,
    pm_depth_no: float = 0.0,
    frac_d: float = 0.4,
    frac_size: int = 60,
    entropy_lookback: int = 60,
    cusum_threshold: float = 0.0005,
    cusum_lookback: int = 120,
    ar_lags: tuple[int, ...] = (1, 5, 15),
) -> dict:
    arr = np.asarray(spot_prices, dtype=np.float64) if spot_prices else np.array([])
    out: dict[str, float] = {}

    out["fracdiff"] = (
        frac_diff_series(arr, d=frac_d, size=frac_size) if arr.size >= frac_size else 0.0
    )

    window = arr[-entropy_lookback:] if arr.size >= entropy_lookback else arr
    out["rets_entropy"] = returns_entropy(window, bins=10)

    out["microprice"] = microprice(pm_yes_bid, pm_yes_ask, pm_depth_yes, pm_depth_no)
    out["book_pressure"] = book_pressure(pm_depth_yes, pm_depth_no)

    if arr.size >= 3:
        with np.errstate(divide="ignore", invalid="ignore"):
            rets = np.diff(np.log(arr))
        rets = rets[np.isfinite(rets)]
        cwindow = rets[-cusum_lookback:] if rets.size >= cusum_lookback else rets
        events = cusum_events(cwindow, cusum_threshold)
        out["cusum_active"] = 1.0 if events and events[-1] == cwindow.size - 1 else 0.0
        out["cusum_event_rate"] = len(events) / max(1, cwindow.size)
    else:
        out["cusum_active"] = 0.0
        out["cusum_event_rate"] = 0.0

    ac = returns_autocorr_multi(arr, lags=ar_lags)
    out.update(ac)

    for k, v in out.items():
        if not math.isfinite(v):
            out[k] = 0.0

    return out
