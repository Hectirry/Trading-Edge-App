"""Driver settle-source dispatch.

Two cases under test:

1. Loader exposes ``provides_settle_prices`` + ``market_outcomes`` →
   driver settles against that dict and *ignores* ``last.chainlink_price``.
2. Regression for the 23-Apr forensic: a chainlink-frozen scenario in
   which the legacy ``_final_price_of`` path produces a 0% win rate, but
   the new path with canonical 1m closes recovers >= 50% win rate.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from trading.engine.backtest_driver import (
    EntryWindowConfig,
    FillConfig,
    run_backtest,
)
from trading.engine.risk import RiskManager
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, OrderType, Side, TickContext

# --------------------------- helpers --------------------------------- #


@dataclass
class _LoaderConfig:
    markets: list[tuple[str, list[TickContext]]]
    settle_prices: dict[str, float] | None = None  # None → no capability


class _FakeLoader:
    def __init__(self, cfg: _LoaderConfig):
        self.cfg = cfg
        if cfg.settle_prices is not None:
            # Marker the driver checks for; mirrors PaperTicksLoader.
            self.provides_settle_prices = True

    def iter_markets(self, from_ts: float, to_ts: float) -> Iterator[tuple[str, list[TickContext]]]:
        yield from self.cfg.markets

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, float]:
        assert self.cfg.settle_prices is not None
        return dict(self.cfg.settle_prices)


class _AlwaysEnterUp(StrategyBase):
    """Enters YES_UP at the first tick whose t_in_window is in the window.
    Avoids dependence on AFML stack so the test exercises driver settle
    only.
    """

    name = "always_enter_up"

    def __init__(self) -> None:
        super().__init__({"params": {}})
        self._entered: set[str] = set()

    def should_enter(self, ctx: TickContext) -> Decision:
        if ctx.market_slug in self._entered:
            return Decision(action=Action.SKIP)
        self._entered.add(ctx.market_slug)
        return Decision(
            action=Action.ENTER,
            side=Side.YES_UP,
            limit_price=ctx.pm_yes_ask,
            order_type=OrderType.MARKET,
        )


def _tick(
    *, ts: float, slug: str, t_in_window: float, close_ts: float, spot: float, chainlink: float
) -> TickContext:
    return TickContext(
        ts=ts,
        market_slug=slug,
        t_in_window=t_in_window,
        window_close_ts=close_ts,
        spot_price=spot,
        chainlink_price=chainlink,
        open_price=70_000.0,
        pm_yes_bid=0.49,
        pm_yes_ask=0.50,
        pm_no_bid=0.49,
        pm_no_ask=0.50,
        pm_depth_yes=100.0,
        pm_depth_no=100.0,
        pm_imbalance=0.0,
        pm_spread_bps=200.0,
        implied_prob_yes=0.50,
        model_prob_yes=0.0,
        edge=0.0,
        z_score=0.0,
        vol_regime="unknown",
        recent_ticks=[],
        t_to_close=max(0.0, close_ts - ts),
        delta_bps=0.0,
    )


def _make_market(slug: str, *, close_ts: float, last_chainlink: float, last_spot: float):
    """Tick sequence with t_in_window covering [120, 240]; only the last
    tick's chainlink_price/spot_price matter for settle.
    """
    ticks = []
    # Synth 121 ticks at 1Hz from t=120s up to t=240s. Keep open_price
    # truthy = 70_000.0 (the field on TickContext) so the driver sees a
    # valid open_price_market without needing the loader override.
    for i in range(0, 121):
        t = 120.0 + i
        ts = close_ts - 300.0 + t
        ticks.append(
            _tick(
                ts=ts,
                slug=slug,
                t_in_window=t,
                close_ts=close_ts,
                spot=last_spot,
                chainlink=last_chainlink,
            )
        )
    return slug, ticks


def _run(loader: _FakeLoader, *, from_ts: float, to_ts: float):
    return run_backtest(
        strategy=_AlwaysEnterUp(),
        loader=loader,  # type: ignore[arg-type]  -- duck-typed to PolybotSQLiteLoader
        from_ts=from_ts,
        to_ts=to_ts,
        stake_usd=5.0,
        fill_cfg=FillConfig(slippage_bps=10.0, fill_probability=1.0, apply_fee_in_backtest=False),
        entry_window=EntryWindowConfig(earliest_entry_t_s=120, latest_entry_t_s=240),
        risk_manager=RiskManager(
            {
                "risk": {
                    "cooldown_seconds": 0,
                    "max_position_size_usd": 100,
                    "daily_loss_limit_usd": 1000,
                    "daily_trade_limit": 999999,
                    "min_edge_bps": 0,
                    "min_z_score": 0.0,
                    "min_pm_depth_usd": 0,
                    "skip_if_spread_bps": 99999,
                    "loss_pause_threshold_usd": 1000,
                    "loss_pause_window_minutes": 30,
                    "loss_pause_duration_minutes": 30,
                }
            }
        ),
        config_used={},
        bypass_risk=True,
    )


# --------------------------- tests ----------------------------------- #


def test_driver_uses_loader_settle_when_capability_present() -> None:
    """With `provides_settle_prices`, settle uses the dict — not chainlink."""
    close_ts = 1_000_300.0
    last_chainlink = 60_000.0  # WAY below open_price (70_000). Frozen.
    last_spot = 60_001.0  # Also below open. legacy path → loss.
    market = _make_market(
        "frozen-market", close_ts=close_ts, last_chainlink=last_chainlink, last_spot=last_spot
    )
    canonical_settle = 70_500.0  # Truth (price went UP).
    loader = _FakeLoader(
        _LoaderConfig(
            markets=[market],
            settle_prices={"frozen-market": canonical_settle},
        )
    )
    res = _run(loader, from_ts=close_ts - 300.0, to_ts=close_ts + 1.0)
    assert res.n_trades == 1
    t = res.trades[0]
    # YES_UP wins iff settle > open. open=70_000, canonical=70_500 → win.
    # If the driver had used `_final_price_of` (chainlink=60_000) it would
    # be a loss. The test asserts the new path won.
    assert t.resolution == "win", f"settle picked the wrong source: {t}"
    assert t.exit_price == 1.0


def test_driver_uses_chainlink_when_capability_absent() -> None:
    """Polybot path: no `provides_settle_prices` → legacy `_final_price_of`."""
    close_ts = 1_000_300.0
    market = _make_market(
        "legacy-market",
        close_ts=close_ts,
        last_chainlink=70_500.0,  # legacy path uses this; > open → win.
        last_spot=60_000.0,
    )
    loader = _FakeLoader(_LoaderConfig(markets=[market]))  # no settle_prices
    res = _run(loader, from_ts=close_ts - 300.0, to_ts=close_ts + 1.0)
    assert res.n_trades == 1
    assert res.trades[0].resolution == "win"


def test_driver_skips_market_when_settle_missing_no_chainlink_fallback() -> None:
    """If the loader exposes the capability but a slug is missing from
    the settle dict, the market is *skipped*, not silently settled with
    chainlink — that fallback is the bug we're fixing."""
    close_ts = 1_000_300.0
    market = _make_market(
        "orphan-market", close_ts=close_ts, last_chainlink=70_500.0, last_spot=70_500.0
    )
    loader = _FakeLoader(
        _LoaderConfig(
            markets=[market],
            settle_prices={},  # capability present, but slug missing
        )
    )
    res = _run(loader, from_ts=close_ts - 300.0, to_ts=close_ts + 1.0)
    assert res.n_trades == 0


def test_regression_chainlink_frozen_4_buckets() -> None:
    """Reproduces the structural shape of the 23-Apr FAIL: 12 markets
    where chainlink_price is frozen at one value (under the canonical
    open) but the canonical Binance settle moved up. Legacy path → 0%
    win; new path → 100% win. Exact win-rate parity isn't the point;
    direction of recovery is.
    """
    open_price = 70_000.0
    canonical_settles = [70_050.0 + i * 5.0 for i in range(12)]  # all up
    chainlink_frozen = open_price - 100.0  # below open → legacy says loss
    markets = []
    settle_dict = {}
    for i, settle in enumerate(canonical_settles):
        slug = f"market-{i:02d}"
        close_ts = 1_000_000.0 + i * 300.0
        # _make_market hardcodes open_price to 70_000 in TickContext.open_price.
        markets.append(
            _make_market(
                slug,
                close_ts=close_ts,
                last_chainlink=chainlink_frozen,
                last_spot=chainlink_frozen + 1.0,
            )
        )
        settle_dict[slug] = settle
    loader_legacy = _FakeLoader(_LoaderConfig(markets=markets))
    res_legacy = _run(loader_legacy, from_ts=999_000.0, to_ts=1_004_000.0)
    legacy_wins = sum(1 for t in res_legacy.trades if t.resolution == "win")
    assert res_legacy.n_trades == 12
    assert legacy_wins == 0, f"legacy win count should be 0, got {legacy_wins}"

    loader_new = _FakeLoader(_LoaderConfig(markets=markets, settle_prices=settle_dict))
    res_new = _run(loader_new, from_ts=999_000.0, to_ts=1_004_000.0)
    new_wins = sum(1 for t in res_new.trades if t.resolution == "win")
    assert res_new.n_trades == 12
    assert new_wins == 12, f"new path win count should be 12, got {new_wins}"
