"""Coverage for `FixedMacroProvider.snapshot_at`.

Locks the closed-bar invariant (no look-ahead from the in-progress 5m
candle) and the ts-asc ordering contract that the bisect lookup relies
on. Pins the exact slice end behavior at the cutoff boundary — this is
where a +/-1 mistake in the bisect call would silently leak the bar
that's still open into the snapshot.

`PostgresMacroProvider` shares the same `_snapshot_from_window` helper,
so its semantics are covered transitively.

Note on `lookback`: the underlying `engine.features.macro.snapshot`
requires ≥ 28 bars, so all tests use `lookback=28`. Closes are given a
small drift so the regime classifier produces a non-None result.
"""

from __future__ import annotations

from trading.strategies.polymarket_btc5m._macro_provider import (
    Candle,
    FixedMacroProvider,
)


def _candles_5m(n: int, *, base_ts: float = 0.0) -> list[Candle]:
    """N consecutive 5m candles. Drifting closes so the snapshot has
    enough variance for the regime classifier to return non-None."""
    return [
        Candle(
            ts=base_ts + i * 300.0,
            high=100.0 + i * 0.5,
            low=99.0 + i * 0.5,
            close=100.0 + i * 0.5,
        )
        for i in range(n)
    ]


def test_snapshot_returns_none_when_fewer_than_lookback_eligible() -> None:
    p = FixedMacroProvider(_candles_5m(27), lookback=28)
    assert p.snapshot_at(as_of_ts=10_000_000.0) is None


def test_snapshot_returns_value_with_exactly_lookback_eligible() -> None:
    p = FixedMacroProvider(_candles_5m(28), lookback=28)
    assert p.snapshot_at(as_of_ts=10_000_000.0) is not None


def test_snapshot_excludes_in_progress_bar_via_300s_guard() -> None:
    # 30 candles spanning ts=0..8700. The candle at ts=8700 is the bar
    # that opened at 8700 and closes at 9000. snapshot_at(as_of_ts=8999)
    # has cutoff=8699 → ts=8700 NOT eligible → only 29 closed bars,
    # which is >= lookback=28 → snapshot. snapshot_at(as_of_ts=8400)
    # has cutoff=8100 → eligible candles 0..27 (count=28) → snapshot.
    # snapshot_at(as_of_ts=8399) has cutoff=8099 → eligible 0..26
    # (count=27) → None.
    p = FixedMacroProvider(_candles_5m(30), lookback=28)
    assert p.snapshot_at(as_of_ts=8999.0) is not None  # 29 eligible
    assert p.snapshot_at(as_of_ts=8400.0) is not None  # 28 eligible (boundary)
    assert p.snapshot_at(as_of_ts=8399.0) is None  # 27 eligible


def test_snapshot_at_window_close_includes_the_just_closed_bar() -> None:
    # The +300 guard means: a candle with ts=T is eligible from
    # as_of_ts=T+300 onwards (the moment its 5m window has ended).
    # Pin: at exactly as_of=T+300 the candle IS in the window.
    candles = _candles_5m(28)  # last candle ts = 8100
    p = FixedMacroProvider(candles, lookback=28)
    last_ts = candles[-1].ts
    assert p.snapshot_at(as_of_ts=last_ts + 300.0) is not None
    # One second earlier and the last candle is still in-progress →
    # only 27 eligible → None.
    assert p.snapshot_at(as_of_ts=last_ts + 299.0) is None


def test_snapshot_window_advances_with_as_of_ts() -> None:
    # Two snapshots with non-overlapping eligible-tail windows must
    # produce different results — proves the slice is computed from
    # as_of_ts, not cached/static.
    candles = _candles_5m(60)  # spans ts 0..17700
    p = FixedMacroProvider(candles, lookback=28)
    snap_early = p.snapshot_at(as_of_ts=8400.0)  # eligible: 0..27 → window [0..27]
    snap_late = p.snapshot_at(as_of_ts=17999.0)  # eligible: 0..58 → window [31..58]
    assert snap_early is not None
    assert snap_late is not None
    assert snap_early != snap_late


def test_unsorted_input_is_normalized_for_bisect_safety() -> None:
    # The provider sorts on init so bisect_right works correctly. If
    # the sort is ever dropped, bisect would silently return wrong
    # slices on shuffled inputs.
    canon = _candles_5m(30)
    shuffled = [
        canon[i]
        for i in (
            5,
            12,
            0,
            29,
            17,
            3,
            22,
            8,
            1,
            14,
            26,
            20,
            7,
            11,
            4,
            28,
            19,
            2,
            15,
            9,
            25,
            13,
            6,
            10,
            21,
            18,
            24,
            27,
            16,
            23,
        )
    ]
    p = FixedMacroProvider(shuffled, lookback=28)
    assert p._tss == sorted(p._tss)
    assert p.candles[0].ts < p.candles[-1].ts
    assert p.snapshot_at(as_of_ts=10_000_000.0) is not None


def test_snapshot_at_returns_none_for_empty_cache() -> None:
    p = FixedMacroProvider([], lookback=28)
    assert p.snapshot_at(as_of_ts=10_000_000.0) is None
