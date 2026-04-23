"""Hand-rolled candle patterns (ADR 0012)."""

from __future__ import annotations

from trading.engine.features.candle_patterns import (
    Candle,
    PatternSignal,
    aggregate_direction,
    detect_1m_micro,
    detect_5m,
)


def _c(o: float, h: float, low: float, c: float) -> Candle:
    return Candle(ts=0.0, open=o, high=h, low=low, close=c)


def test_detect_5m_too_few_bars_returns_empty() -> None:
    assert detect_5m([_c(100, 101, 99, 100)]) == []


def test_doji_detected() -> None:
    # Tiny body, wide range — last bar.
    cs = [
        _c(100, 100.5, 99.5, 100.1),
        _c(100.1, 100.2, 99.9, 100),
        _c(100, 102, 98, 100),
    ]
    sigs = detect_5m(cs)
    assert any(s.name == "doji" for s in sigs)


def test_bullish_engulfing_detected() -> None:
    cs = [
        _c(100, 101, 99, 100),
        _c(101, 101.5, 99, 99.5),
        _c(99.2, 102, 98.8, 101.8),
    ]
    sigs = detect_5m(cs)
    eng = [s for s in sigs if s.name == "engulfing"]
    assert eng and eng[0].direction == 1


def test_bearish_engulfing_detected() -> None:
    cs = [
        _c(100, 101, 99, 100),
        _c(100, 101.5, 99.5, 101),
        _c(101.2, 101.4, 98, 99.0),
    ]
    sigs = detect_5m(cs)
    eng = [s for s in sigs if s.name == "engulfing"]
    assert eng and eng[0].direction == -1


def test_hammer_detected() -> None:
    cs = [
        _c(100, 100.5, 99.5, 100),
        _c(100.1, 100.2, 99.9, 100),
        _c(100.0, 100.2, 95.0, 100.15),
    ]
    sigs = detect_5m(cs)
    assert any(s.name == "hammer" and s.direction == 1 for s in sigs)


def test_shooting_star_detected() -> None:
    cs = [
        _c(100, 100.5, 99.5, 100),
        _c(100.1, 100.2, 99.9, 100),
        _c(100.1, 105.0, 99.95, 100.0),
    ]
    sigs = detect_5m(cs)
    assert any(s.name == "shooting_star" and s.direction == -1 for s in sigs)


def test_morning_star_detected() -> None:
    cs = [
        _c(100, 100.5, 95, 95.5),
        _c(95.4, 95.6, 95.1, 95.5),
        _c(95.8, 100, 95.7, 99.8),
    ]
    sigs = detect_5m(cs)
    assert any(s.name == "morning_star" for s in sigs)


def test_evening_star_detected() -> None:
    cs = [
        _c(95, 100, 94.5, 99.5),
        _c(99.5, 99.8, 99.3, 99.6),
        _c(99.5, 99.6, 95, 95.5),
    ]
    sigs = detect_5m(cs)
    assert any(s.name == "evening_star" for s in sigs)


def test_detect_1m_micro_skips_three_bar_patterns() -> None:
    cs = [
        _c(100, 100.5, 95, 95.5),
        _c(95.4, 95.6, 95.1, 95.5),
        _c(95.8, 100, 95.7, 99.8),
    ]
    sigs = detect_1m_micro(cs)
    assert not any(s.name in ("morning_star", "evening_star", "advance_block") for s in sigs)


def test_aggregate_direction_empty() -> None:
    assert aggregate_direction([]) == (0, 0.0)


def test_aggregate_direction_bull() -> None:
    sigs = [PatternSignal(name="hammer", direction=1, strength=0.8)]
    assert aggregate_direction(sigs) == (1, 0.8)


def test_aggregate_direction_mixed_cancels() -> None:
    sigs = [
        PatternSignal(name="hammer", direction=1, strength=0.5),
        PatternSignal(name="shooting_star", direction=-1, strength=0.5),
    ]
    assert aggregate_direction(sigs) == (0, 0.0)


def test_aggregate_direction_ignores_doji() -> None:
    sigs = [
        PatternSignal(name="doji", direction=0, strength=1.0),
        PatternSignal(name="hammer", direction=1, strength=0.3),
    ]
    assert aggregate_direction(sigs) == (1, 0.3)


def test_aggregate_direction_saturates() -> None:
    sigs = [
        PatternSignal(name="a", direction=1, strength=0.8),
        PatternSignal(name="b", direction=1, strength=0.8),
    ]
    assert aggregate_direction(sigs)[1] == 1.0
