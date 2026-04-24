"""Settle watchdog (post-window settle path) + _settle_from_values.

Covers the bug where ticks stop arriving after t_in_window=300 so the
engine used to leave paper positions "open forever" and Grafana showed
entry_price only. The watchdog now settles via last paper_tick or an
OHLCV fallback.
"""

from __future__ import annotations

import pytest

from trading.engine.types import Side
from trading.paper.driver import PaperDriver
from trading.paper.exec_client import Position


class _Strat:
    name = "test_strategy"


class _Risk:
    def on_trade_closed(self, pnl, *, now):
        pass  # noqa: D401


class _Heartbeat:
    n_open_positions = 0
    n_trades_today = 0


class _Exec:
    def __init__(self):
        self.calls = []

    async def settle(self, pos, *, settle_ts, settle_price, outcome_went_up):
        self.calls.append((pos.client_order_id, settle_ts, settle_price, outcome_went_up))
        # Mimic real engine.fill_model.settle output shape.
        resolution = "YES_UP" if outcome_went_up else "YES_DOWN"
        pnl = 10.0 if outcome_went_up else -5.0
        return resolution, 1.0, pnl


class _TG:
    async def send(self, *_args, **_kw):
        pass


class _Cfg:
    daily_alert_pnl_threshold = -1000.0
    daily_pause_pnl_threshold = -1000.0
    stake_usd = 5.0


def _bare_driver() -> PaperDriver:
    d = PaperDriver.__new__(PaperDriver)
    d.strategy = _Strat()
    d.risk = _Risk()
    d.exec = _Exec()
    d.tg = _TG()
    d.heartbeat = _Heartbeat()
    d.cfg = _Cfg()
    d._paused = False
    d._control_channel = "tea:control:test_strategy"
    d._indicators = {}
    d._recent_ticks = {}
    d._open_positions = {}
    d._trade_taken = set()
    d._today = ""
    d._daily_pnl = 0.0
    d._daily_trades = 0
    d._kill_switch_last_state = False
    d._eval_counts = {}
    d._eval_skip_reasons = {}
    return d


def _pos(slug: str = "btc-updown-5m-1", close_ts: float = 1_000_000.0) -> Position:
    return Position(
        market_slug=slug,
        condition_id=f"cid-{slug}",
        side=Side.YES_UP,
        entry_ts=close_ts - 60,
        entry_price=0.50,
        stake_usd=5.0,
        slippage=0.001,
        fee=0.05,
        client_order_id=f"coid-{slug}",
        window_close_ts=close_ts,
        open_price=70_000.0,
        strategy_id="test_strategy",
    )


@pytest.mark.asyncio
async def test_settle_from_values_writes_and_cleans_up() -> None:
    d = _bare_driver()
    d._open_positions["slug-x"] = _pos(slug="slug-x")
    d._indicators["slug-x"] = object()  # anything; _cleanup_market pops it
    await d._settle_from_values(
        "slug-x",
        settle_ts=1_000_100.0,
        settle_price=70_500.0,
        went_up=True,
        source="unit",
    )
    assert "slug-x" not in d._open_positions
    assert "slug-x" not in d._indicators
    assert d._daily_pnl == 10.0
    assert d.exec.calls[0][2] == 70_500.0
    assert d.exec.calls[0][3] is True


@pytest.mark.asyncio
async def test_settle_from_values_noop_if_position_missing() -> None:
    d = _bare_driver()
    await d._settle_from_values(
        "ghost",
        settle_ts=1.0,
        settle_price=70_000.0,
        went_up=False,
        source="unit",
    )
    assert d.exec.calls == []


@pytest.mark.asyncio
async def test_watchdog_uses_paper_tick_when_available(monkeypatch) -> None:
    d = _bare_driver()
    pos = _pos(slug="slug-a")
    d._open_positions["slug-a"] = pos

    async def fake_paper_tick(cid, close_ts):
        assert cid == pos.condition_id
        return 70_800.0, close_ts + 3, "paper_tick"

    async def fake_first_tick(cid, *, window_close_ts=None):
        assert cid == pos.condition_id
        return 70_000.0, "paper_tick_first_spot"

    async def fake_ohlcv(close_ts):
        raise AssertionError("should not call ohlcv when paper_tick works")

    d._last_paper_tick_price = fake_paper_tick
    d._first_paper_tick_spot = fake_first_tick
    d._ohlcv_close_at = fake_ohlcv
    # 30 s past close → above the 15 s threshold.
    await d._settle_via_watchdog("slug-a", pos, now=pos.window_close_ts + 30)
    assert "slug-a" not in d._open_positions
    _, settle_ts, price, went_up = d.exec.calls[0]
    assert price == 70_800.0
    assert went_up is True


@pytest.mark.asyncio
async def test_watchdog_falls_back_to_ohlcv_after_120s() -> None:
    d = _bare_driver()
    pos = _pos(slug="slug-b", close_ts=2_000_000.0)
    d._open_positions["slug-b"] = pos

    async def fake_paper_tick(*_a, **_k):
        return None, 0.0, "no_paper_tick"

    async def fake_first_tick(_cid, *, window_close_ts=None):
        return None, "no_first_tick"

    async def fake_ohlcv(close_ts):
        if close_ts == pos.window_close_ts:
            return 69_500.0, close_ts
        if close_ts == pos.window_close_ts - 300:
            return 70_000.0, close_ts
        return None, close_ts

    d._last_paper_tick_price = fake_paper_tick
    d._first_paper_tick_spot = fake_first_tick
    d._ohlcv_close_at = fake_ohlcv
    # 130 s past close → above the 120 s ohlcv threshold.
    await d._settle_via_watchdog("slug-b", pos, now=pos.window_close_ts + 130)
    _, _, price, went_up = d.exec.calls[0]
    assert price == 69_500.0
    assert went_up is False  # 69 500 < 70 000 open


@pytest.mark.asyncio
async def test_watchdog_uses_ohlcv_open_when_first_tick_missing() -> None:
    d = _bare_driver()
    pos = _pos(slug="slug-c", close_ts=3_000_000.0)
    d._open_positions["slug-c"] = pos

    async def fake_paper_tick(*_a, **_k):
        return 70_800.0, pos.window_close_ts, "paper_tick"

    async def fake_first_tick(_cid, *, window_close_ts=None):
        return None, "no_first_tick"

    async def fake_ohlcv(close_ts):
        if close_ts == pos.window_close_ts - 300:
            return 70_000.0, close_ts
        return None, close_ts

    d._last_paper_tick_price = fake_paper_tick
    d._first_paper_tick_spot = fake_first_tick
    d._ohlcv_close_at = fake_ohlcv
    await d._settle_via_watchdog("slug-c", pos, now=pos.window_close_ts + 30)
    _, _, price, went_up = d.exec.calls[0]
    assert price == 70_800.0
    assert went_up is True  # 70 800 > 70 000 ohlcv open fallback


@pytest.mark.asyncio
async def test_watchdog_drops_after_10min_without_prices() -> None:
    d = _bare_driver()
    pos = _pos(slug="slug-d", close_ts=4_000_000.0)
    d._open_positions["slug-d"] = pos

    async def fake_paper_tick(*_a, **_k):
        return None, 0.0, "no_paper_tick"

    async def fake_ohlcv(*_a, **_k):
        return None, 0.0

    d._last_paper_tick_price = fake_paper_tick
    d._ohlcv_close_at = fake_ohlcv
    # 650 s past close → past the 600 s give-up threshold.
    await d._settle_via_watchdog("slug-d", pos, now=pos.window_close_ts + 650)
    # No settle call, position dropped.
    assert d.exec.calls == []
    assert "slug-d" not in d._open_positions


@pytest.mark.asyncio
async def test_watchdog_retries_before_10min_when_no_price() -> None:
    d = _bare_driver()
    pos = _pos(slug="slug-e", close_ts=5_000_000.0)
    d._open_positions["slug-e"] = pos

    async def fake_paper_tick(*_a, **_k):
        return None, 0.0, "no_paper_tick"

    async def fake_ohlcv(*_a, **_k):
        return None, 0.0

    d._last_paper_tick_price = fake_paper_tick
    d._ohlcv_close_at = fake_ohlcv
    # 60 s past close → below 120 s ohlcv threshold → still pending.
    await d._settle_via_watchdog("slug-e", pos, now=pos.window_close_ts + 60)
    assert "slug-e" in d._open_positions
    assert d.exec.calls == []
