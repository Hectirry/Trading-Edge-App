"""Coverage for `PaperTicksLoader.iter_markets` and `_row_to_tick`.

`_fetch_window` is the only DB seam — we patch it so the test exercises
just the in-memory grouping/yield-ordering and TickContext construction.
This locks in the contracts callers (run_backtest) rely on:

- markets emit in ascending order of MIN(ts) per slug
- intra-market ticks stay ts-ascending
- ohlcv_open from the bulk lookup overrides the row's open_price
- ohlcv_open absent → fall back to row.open_price → row.spot_price
- delta_bps uses the resolved open_price (not the raw row column)
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.paper.backtest_loader import PaperTicksLoader, _row_to_tick


def _row(*, slug: str, ts_unix: float, window_close: int, **overrides) -> dict:
    """Build a paper_ticks-like row dict. Defaults are realistic but
    irrelevant fields are zeroed; overrides let each test be explicit."""
    base = {
        "ts": datetime.fromtimestamp(ts_unix, tz=UTC),
        "market_slug": slug,
        "t_in_window": max(0.0, ts_unix - (window_close - 300)),
        "window_close_ts": window_close,
        "spot_price": 70000.0,
        "chainlink_price": 0.0,
        "open_price": 0.0,
        "pm_yes_bid": 0.50,
        "pm_yes_ask": 0.51,
        "pm_no_bid": 0.49,
        "pm_no_ask": 0.50,
        "pm_depth_yes": 100.0,
        "pm_depth_no": 100.0,
        "pm_imbalance": 0.0,
        "pm_spread_bps": 100.0,
        "implied_prob_yes": 0.50,
    }
    base.update(overrides)
    return base


# --- _row_to_tick ----------------------------------------------------------


def test_row_to_tick_uses_ohlcv_open_when_provided() -> None:
    # ohlcv_open=70010 wins over row.open_price=70005 (which is the
    # potentially-stale recorder snapshot). delta_bps must be derived
    # from the OHLCV open, not the row's open_price.
    r = _row(
        slug="m1", ts_unix=1_000_300, window_close=1_000_300, spot_price=70100.0, open_price=70005.0
    )
    tick = _row_to_tick(r, ohlcv_open=70010.0)
    assert tick.open_price == 70010.0
    expected_bps = (70100.0 - 70010.0) / 70010.0 * 10000.0
    assert abs(tick.delta_bps - expected_bps) < 1e-6


def test_row_to_tick_falls_back_to_row_open_when_ohlcv_missing() -> None:
    # Ingest gap → no OHLCV. Loader falls back to the recorder's
    # open_price snapshot if it has a valid (>0) value.
    r = _row(
        slug="m1", ts_unix=1_000_100, window_close=1_000_300, spot_price=70080.0, open_price=70000.0
    )
    tick = _row_to_tick(r, ohlcv_open=None)
    assert tick.open_price == 70000.0
    expected_bps = (70080.0 - 70000.0) / 70000.0 * 10000.0
    assert abs(tick.delta_bps - expected_bps) < 1e-6


def test_row_to_tick_falls_back_to_spot_when_both_open_sources_missing() -> None:
    # Recorder didn't latch open_price (open_price=0) AND OHLCV gap.
    # Last-resort: use spot. delta_bps then becomes 0 (spot==open).
    r = _row(
        slug="m1", ts_unix=1_000_50, window_close=1_000_300, spot_price=70050.0, open_price=0.0
    )
    tick = _row_to_tick(r, ohlcv_open=None)
    assert tick.open_price == 70050.0
    assert tick.delta_bps == 0.0


def test_row_to_tick_treats_zero_ohlcv_open_as_missing() -> None:
    # ohlcv_open == 0 must not be used (would zero-divide and bias the
    # backtest). Loader falls through to row open / spot.
    r = _row(
        slug="m1", ts_unix=1_000_100, window_close=1_000_300, spot_price=70100.0, open_price=70010.0
    )
    tick = _row_to_tick(r, ohlcv_open=0.0)
    assert tick.open_price == 70010.0


def test_row_to_tick_t_to_close_is_window_close_minus_ts() -> None:
    r = _row(slug="m1", ts_unix=1_000_120, window_close=1_000_300)
    tick = _row_to_tick(r, ohlcv_open=70000.0)
    assert tick.t_to_close == 180.0


# --- iter_markets ----------------------------------------------------------


def _patch_fetch_window(loader: PaperTicksLoader, ticks_rows, ohlcv_opens):
    """Bypass DB by replacing `_fetch_window` with a coroutine returning
    the canned data."""

    async def _fake(_from, _to):
        return ticks_rows, ohlcv_opens

    loader._fetch_window = _fake  # type: ignore[method-assign]


def test_iter_markets_groups_by_slug_in_first_tick_order() -> None:
    # Two markets interleaved in the underlying ts-ASC tick stream:
    # m_early opens at t=100, m_late opens at t=200. The loader must
    # yield m_early first regardless of which slug name sorts higher.
    rows = [
        _row(slug="m_z", ts_unix=100.0, window_close=400),
        _row(slug="m_a", ts_unix=200.0, window_close=500),
        _row(slug="m_z", ts_unix=210.0, window_close=400),
        _row(slug="m_a", ts_unix=300.0, window_close=500),
        _row(slug="m_z", ts_unix=350.0, window_close=400),
    ]
    loader = PaperTicksLoader(dsn="ignored")
    _patch_fetch_window(loader, rows, ohlcv_opens={400: 70000.0, 500: 71000.0})

    out = list(loader.iter_markets(0, 1000))
    assert [slug for slug, _ in out] == ["m_z", "m_a"]
    # Per-market ticks preserve ts-ascending order.
    m_z_ts = [t.ts for t in out[0][1]]
    m_a_ts = [t.ts for t in out[1][1]]
    assert m_z_ts == [100.0, 210.0, 350.0]
    assert m_a_ts == [200.0, 300.0]


def test_iter_markets_passes_correct_ohlcv_open_per_market() -> None:
    # Two markets with different window_close_ts; each must get its
    # own ohlcv_open from the bulk dict (not the other's).
    rows = [
        _row(slug="m1", ts_unix=10.0, window_close=300, spot_price=71000.0),
        _row(slug="m2", ts_unix=20.0, window_close=600, spot_price=72000.0),
    ]
    loader = PaperTicksLoader(dsn="ignored")
    _patch_fetch_window(loader, rows, ohlcv_opens={300: 70500.0, 600: 71500.0})

    out = dict(loader.iter_markets(0, 1000))
    assert out["m1"][0].open_price == 70500.0
    assert out["m2"][0].open_price == 71500.0


def test_iter_markets_skips_rows_with_empty_slug() -> None:
    # Defensive: paper_ticks rarely has NULL/empty market_slug, but the
    # SELECT doesn't filter — so the loader must.
    rows = [
        _row(slug="", ts_unix=10.0, window_close=300),
        _row(slug="m1", ts_unix=20.0, window_close=300),
    ]
    loader = PaperTicksLoader(dsn="ignored")
    _patch_fetch_window(loader, rows, ohlcv_opens={300: 70000.0})

    out = list(loader.iter_markets(0, 1000))
    assert [slug for slug, _ in out] == ["m1"]


def test_iter_markets_yields_nothing_for_empty_window() -> None:
    loader = PaperTicksLoader(dsn="ignored")
    _patch_fetch_window(loader, ticks_rows=[], ohlcv_opens={})
    assert list(loader.iter_markets(0, 1000)) == []
