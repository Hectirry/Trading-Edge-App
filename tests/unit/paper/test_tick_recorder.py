"""Coverage for `_is_in_publishing_window`, the gate that stops the
recorder from emitting `t_in_window=0` ticks for the ~100 upcoming
markets that `feeds._refresh_once` seeds into `state.markets`.

Pre-fix: the recorder filtered only on `window_close_ts > now`, so every
1 Hz spot tick fanned out into a publish + paper_ticks insert per
upcoming market — even ones whose window opens hours from now.
"""

from trading.paper.tick_recorder import WINDOW_OPEN_LEAD_S, _is_in_publishing_window


def _close_ts(now: float, *, opens_in: float) -> float:
    """Helper: build a window_close_ts whose window_open is `now + opens_in`.

    `opens_in` negative ⇒ window already opened ago. Positive ⇒ opens later.
    """
    return now + opens_in + 300.0


def test_skips_far_future_market():
    now = 1_700_000_000.0
    # window opens in 1 hour; far outside the lead margin
    assert not _is_in_publishing_window(_close_ts(now, opens_in=3600.0), now)


def test_includes_active_mid_window_market():
    now = 1_700_000_000.0
    # 150 s into a 300 s window
    assert _is_in_publishing_window(_close_ts(now, opens_in=-150.0), now)


def test_skips_just_closed_market():
    now = 1_700_000_000.0
    # closed 1 s ago
    assert not _is_in_publishing_window(now - 1.0, now)


def test_excludes_window_close_boundary_strictly():
    # Strict upper bound preserves prior behavior (`window_close_ts > now`).
    # The recorder's inner `if t_in_window > 300: continue` would otherwise
    # silently swallow these anyway; making the filter strict keeps the
    # contract obvious.
    now = 1_700_000_000.0
    assert not _is_in_publishing_window(now, now)


def test_includes_window_open_boundary():
    # At exactly window_open, the recorder must already be ticking so the
    # open-price capture (lines 75-80 in tick_recorder.py) latches.
    now = 1_700_000_000.0
    assert _is_in_publishing_window(_close_ts(now, opens_in=0.0), now)


def test_includes_market_within_lead_margin():
    # Lead margin protects against sub-second clock skew between the
    # master clock and Polymarket's window scheduling.
    now = 1_700_000_000.0
    just_inside = _close_ts(now, opens_in=WINDOW_OPEN_LEAD_S - 0.1)
    assert _is_in_publishing_window(just_inside, now)


def test_excludes_market_just_outside_lead_margin():
    now = 1_700_000_000.0
    just_outside = _close_ts(now, opens_in=WINDOW_OPEN_LEAD_S + 0.1)
    assert not _is_in_publishing_window(just_outside, now)


def test_realistic_upcoming_batch_keeps_only_active_one():
    # Mirrors what `feeds._refresh_once` produces: one market currently
    # in its 5-min window plus a long tail of upcoming ones (Polymarket
    # publishes the next ~100 5-min btc-updown markets ahead of time).
    now = 1_700_000_000.0
    upcoming_close_tss = [
        _close_ts(now, opens_in=-120.0),  # ACTIVE: 2 min into window
        _close_ts(now, opens_in=300.0),  # opens in 5 min
        _close_ts(now, opens_in=600.0),  # opens in 10 min
        _close_ts(now, opens_in=3600.0),  # opens in 1 h
        _close_ts(now, opens_in=86400.0),  # opens tomorrow
    ]
    kept = [c for c in upcoming_close_tss if _is_in_publishing_window(c, now)]
    assert len(kept) == 1
