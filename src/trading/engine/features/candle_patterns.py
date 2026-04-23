"""Hand-rolled candle-pattern detection (ADR 0012).

TA-Lib's C library is painful to install in slim Python images, so we
implement the small subset the contest strategies need directly. Each
detector consumes a tail of ``Candle`` rows and emits zero-or-one
``PatternSignal`` for the most recent bar. Magnitudes are tuned to a
0–1 strength range so downstream aggregators can treat every detector
uniformly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PatternSignal:
    name: str
    direction: int       # -1 = bearish, 0 = neutral, +1 = bullish
    strength: float      # [0, 1]


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _range(c: Candle) -> float:
    return max(1e-9, c.high - c.low)


def _is_bull(c: Candle) -> bool:
    return c.close > c.open


def _is_bear(c: Candle) -> bool:
    return c.close < c.open


def _detect_doji(c: Candle) -> PatternSignal | None:
    r = _range(c)
    body_ratio = _body(c) / r
    # Body ≤ 10% of range → doji. Direction 0 (pattern indicates
    # indecision, strategy treats it as "weak signal, penalty").
    if body_ratio <= 0.10:
        return PatternSignal(name="doji", direction=0, strength=1.0 - body_ratio * 10)
    return None


def _detect_engulfing(prev: Candle, cur: Candle) -> PatternSignal | None:
    # Bullish engulfing: prev red, cur green, cur body engulfs prev.
    if _is_bear(prev) and _is_bull(cur) and cur.close >= prev.open and cur.open <= prev.close:
        return PatternSignal(name="engulfing", direction=1, strength=1.0)
    # Bearish engulfing: prev green, cur red, cur body engulfs prev.
    if _is_bull(prev) and _is_bear(cur) and cur.open >= prev.close and cur.close <= prev.open:
        return PatternSignal(name="engulfing", direction=-1, strength=1.0)
    return None


def _detect_hammer(c: Candle) -> PatternSignal | None:
    r = _range(c)
    body = _body(c)
    lower_wick = min(c.open, c.close) - c.low
    upper_wick = c.high - max(c.open, c.close)
    # Small body high on the range + long lower wick + tiny upper wick.
    if r <= 0 or body / r > 0.40:
        return None
    if lower_wick >= 2.0 * body and upper_wick <= 0.1 * r and _is_bull(c):
        strength = min(1.0, lower_wick / (3.0 * max(body, 1e-9)))
        return PatternSignal(name="hammer", direction=1, strength=strength)
    return None


def _detect_shooting_star(c: Candle) -> PatternSignal | None:
    r = _range(c)
    body = _body(c)
    lower_wick = min(c.open, c.close) - c.low
    upper_wick = c.high - max(c.open, c.close)
    if r <= 0 or body / r > 0.40:
        return None
    if upper_wick >= 2.0 * body and lower_wick <= 0.1 * r and _is_bear(c):
        strength = min(1.0, upper_wick / (3.0 * max(body, 1e-9)))
        return PatternSignal(name="shooting_star", direction=-1, strength=strength)
    return None


def _detect_morning_star(candles: Sequence[Candle]) -> PatternSignal | None:
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    mid = (a.open + a.close) / 2
    if _is_bear(a) and _body(b) < 0.3 * _body(a) and _is_bull(c) and c.close > mid:
        return PatternSignal(name="morning_star", direction=1, strength=0.9)
    return None


def _detect_evening_star(candles: Sequence[Candle]) -> PatternSignal | None:
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    mid = (a.open + a.close) / 2
    if _is_bull(a) and _body(b) < 0.3 * _body(a) and _is_bear(c) and c.close < mid:
        return PatternSignal(name="evening_star", direction=-1, strength=0.9)
    return None


def _detect_advance_block(candles: Sequence[Candle]) -> PatternSignal | None:
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    # Three bull bars with shrinking bodies + long upper wicks → exhaustion.
    if not (_is_bull(a) and _is_bull(b) and _is_bull(c)):
        return None
    bodies = [_body(a), _body(b), _body(c)]
    if bodies[0] > bodies[1] > bodies[2] and c.high > b.high > a.high:
        upper = c.high - max(c.open, c.close)
        if upper > _body(c):
            return PatternSignal(name="advance_block", direction=-1, strength=0.7)
    return None


def detect_5m(candles: Sequence[Candle]) -> list[PatternSignal]:
    """Detect the contest-relevant patterns on the last ≥ 3 candles."""
    if len(candles) < 3:
        return []
    out: list[PatternSignal] = []
    cur = candles[-1]
    prev = candles[-2]
    for sig in (_detect_doji(cur),
                _detect_engulfing(prev, cur),
                _detect_hammer(cur),
                _detect_shooting_star(cur),
                _detect_morning_star(candles),
                _detect_evening_star(candles),
                _detect_advance_block(candles)):
        if sig is not None:
            out.append(sig)
    return out


def detect_1m_micro(candles: Sequence[Candle]) -> list[PatternSignal]:
    """Micro-pattern detection on 1m bars. Uses the same detectors
    minus the three-bar ones (morning/evening star, advance block)."""
    if len(candles) < 2:
        return []
    out: list[PatternSignal] = []
    cur = candles[-1]
    prev = candles[-2]
    for sig in (
        _detect_doji(cur),
        _detect_engulfing(prev, cur),
        _detect_hammer(cur),
        _detect_shooting_star(cur),
    ):
        if sig is not None:
            out.append(sig)
    return out


def aggregate_direction(signals: Sequence[PatternSignal]) -> tuple[int, float]:
    """Reduce a list of signals to a single (direction, confidence).

    Sum signed strengths; return ``(sign, abs(sum) clipped to 1)``.
    Empty → ``(0, 0.0)``. Signals with ``direction == 0`` (doji)
    contribute to neither sign; they are still emitted so v1-B can
    apply its own penalty.
    """
    if not signals:
        return 0, 0.0
    total = sum(s.direction * s.strength for s in signals if s.direction != 0)
    if total == 0:
        return 0, 0.0
    direction = 1 if total > 0 else -1
    confidence = min(1.0, abs(total))
    return direction, confidence
