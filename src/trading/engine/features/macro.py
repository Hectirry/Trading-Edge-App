"""Macro features over 5 m Binance candles (ADR 0011)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Regime = Literal["uptrend", "downtrend", "range"]


@dataclass(frozen=True)
class MacroSnapshot:
    ema8: float
    ema34: float
    adx_14: float
    consecutive_same_dir: int  # signed: positive = up streak, negative = down streak
    regime: Regime
    ema8_vs_ema34_pct: float


def ema(values: Sequence[float], period: int) -> float:
    """Exponential moving average; returns 0 on insufficient data."""
    if period < 1 or len(values) < period:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    # seed with SMA of the first ``period`` values, standard convention.
    seed = sum(values[:period]) / period
    out = seed
    for v in values[period:]:
        out = alpha * v + (1.0 - alpha) * out
    return out


def adx_14(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> float:
    """Wilder's ADX(14). Needs ≥ 28 bars (14 warm-up + 14 smooth)."""
    n = 14
    if len(closes) < 2 * n or len(highs) != len(closes) or len(lows) != len(closes):
        return 0.0
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def _wilder(xs: Sequence[float]) -> list[float]:
        if len(xs) < n:
            return []
        out = [sum(xs[:n])]
        for v in xs[n:]:
            out.append(out[-1] - out[-1] / n + v)
        return out

    plus_smooth = _wilder(plus_dm)
    minus_smooth = _wilder(minus_dm)
    tr_smooth = _wilder(tr)
    if not tr_smooth:
        return 0.0
    dxs: list[float] = []
    for ps, ms, trs in zip(plus_smooth, minus_smooth, tr_smooth, strict=True):
        if trs <= 0:
            continue
        plus_di = 100.0 * ps / trs
        minus_di = 100.0 * ms / trs
        denom = plus_di + minus_di
        if denom == 0:
            continue
        dxs.append(100.0 * abs(plus_di - minus_di) / denom)
    if len(dxs) < n:
        return 0.0
    # Wilder-smoothed ADX: SMA of first n DX, then recursive.
    adx = sum(dxs[:n]) / n
    for v in dxs[n:]:
        adx = (adx * (n - 1) + v) / n
    return adx


def consecutive_same_direction(closes: Sequence[float]) -> int:
    """Signed count of consecutive bars with the same sign of close change.

    Positive for an up streak, negative for a down streak.
    """
    if len(closes) < 2:
        return 0
    streak = 0
    sign = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        s = 1 if diff > 0 else (-1 if diff < 0 else 0)
        if s == 0:
            break
        if sign == 0:
            sign = s
            streak = 1
            continue
        if s != sign:
            break
        streak += 1
    return streak * sign


def classify_regime(
    ema_fast: float,
    ema_slow: float,
    adx_val: float,
    consec: int,
    *,
    adx_threshold: float = 20.0,
    consecutive_min: int = 2,
) -> Regime:
    if ema_fast > ema_slow and adx_val >= adx_threshold and consec >= consecutive_min:
        return "uptrend"
    if ema_fast < ema_slow and adx_val >= adx_threshold and -consec >= consecutive_min:
        return "downtrend"
    return "range"


def snapshot(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    adx_threshold: float = 20.0,
    consecutive_min: int = 2,
) -> MacroSnapshot | None:
    """Compute a snapshot from ≥ 28 bars; returns None if insufficient."""
    if len(closes) < 28 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    ema_fast = ema(closes, 8)
    ema_slow = ema(closes, 34) if len(closes) >= 34 else ema(closes, min(len(closes), 34))
    adx_val = adx_14(highs, lows, closes)
    consec = consecutive_same_direction(closes)
    regime = classify_regime(
        ema_fast, ema_slow, adx_val, consec,
        adx_threshold=adx_threshold, consecutive_min=consecutive_min,
    )
    pct = 0.0 if ema_slow == 0 else (ema_fast - ema_slow) / ema_slow * 100.0
    return MacroSnapshot(
        ema8=ema_fast,
        ema34=ema_slow,
        adx_14=adx_val,
        consecutive_same_dir=consec,
        regime=regime,
        ema8_vs_ema34_pct=pct,
    )
