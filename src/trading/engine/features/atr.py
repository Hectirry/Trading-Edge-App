"""Average True Range (Wilder) — OHLC candle volatility.

Used by grid_atr_adaptive_v1 to size grid steps to recent range. ATR is
a range-based volatility measure over OHLC candles, distinct from the
tick-level RealizedVol / EWMAVol in engine/indicators.py.

True range:  TR_n = max(high - low, |high - prev_close|, |low - prev_close|)
ATR (Wilder): ATR_n = ((period - 1) * ATR_{n-1} + TR_n) / period
Seed:        ATR_period = simple mean of TR over first `period` bars.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class ATR:
    """Wilder-smoothed ATR. Stateful, one instance per series."""

    period: int = 14
    value: float = 0.0
    _trs: deque[float] = field(default_factory=lambda: deque(maxlen=1024))
    _prev_close: float | None = None
    _seeded: bool = False

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        self._trs.append(tr)

        if not self._seeded:
            if len(self._trs) < self.period:
                self.value = 0.0
                return 0.0
            self.value = sum(self._trs) / self.period
            self._seeded = True
            return self.value

        self.value = ((self.period - 1) * self.value + tr) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return self._seeded


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    """Batch ATR for a full OHLC series. Returns final ATR value.

    Returns 0.0 if fewer than ``period + 1`` bars (need one prior close to
    seed the first TR that uses ``|high - prev_close|``).
    """
    if len(highs) != len(lows) or len(highs) != len(closes):
        raise ValueError("highs/lows/closes must have equal length")
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(highs) < period + 1:
        return 0.0

    atr = ATR(period=period)
    for h, lo, c in zip(highs, lows, closes, strict=True):
        atr.update(h, lo, c)
    return atr.value
