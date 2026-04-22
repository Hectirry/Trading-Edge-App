from datetime import UTC, datetime

from trading.ingest.polymarket.slug import (
    SLUG_PREFIX,
    WINDOW_SECONDS,
    current_window,
    next_window,
    window_for,
    windows_between,
)


def test_window_for_rounds_down_to_5min():
    ts = datetime(2026, 1, 1, 12, 7, 33, tzinfo=UTC)  # 12:07:33 -> window 12:05 to 12:10
    w = window_for(ts)
    expected_open = datetime(2026, 1, 1, 12, 5, tzinfo=UTC).timestamp()
    expected_close = datetime(2026, 1, 1, 12, 10, tzinfo=UTC).timestamp()
    assert w.open_ts == int(expected_open)
    assert w.close_ts == int(expected_close)
    assert w.slug == f"{SLUG_PREFIX}{int(expected_close)}"


def test_window_on_exact_boundary():
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    w = window_for(ts)
    assert w.open_ts == int(ts.timestamp())
    assert w.close_ts == w.open_ts + WINDOW_SECONDS


def test_windows_between_covers_full_range():
    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    until = datetime(2026, 1, 1, 12, 30, 0, tzinfo=UTC)
    ws = windows_between(since, until)
    # windows close at 12:05, 12:10, 12:15, 12:20, 12:25, 12:30 = 6 windows
    assert len(ws) == 6
    assert ws[0].slug == f"{SLUG_PREFIX}{int(datetime(2026,1,1,12,5,tzinfo=UTC).timestamp())}"
    assert ws[-1].slug == f"{SLUG_PREFIX}{int(until.timestamp())}"


def test_current_window_alignment_is_multiple_of_300():
    w = current_window()
    assert w.open_ts % WINDOW_SECONDS == 0
    assert w.close_ts - w.open_ts == WINDOW_SECONDS


def test_next_window_after_current():
    now = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    cur = window_for(now)
    nxt = next_window(now)
    assert nxt.open_ts == cur.close_ts
