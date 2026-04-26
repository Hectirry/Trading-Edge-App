"""Monte Carlo evaluation tests.

Two flavors covered:
  * bootstrap_metrics + permutation p-value on synthetic trade vectors.
  * block_bootstrap_replay against a tiny in-memory loader to exercise
    the full driver path without DB or polybot SQLite.
"""

from __future__ import annotations

import math

import pytest

from trading.engine.backtest_driver import (
    BacktestTrade,
    EntryWindowConfig,
    FillConfig,
)
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext
from trading.research.monte_carlo import (
    block_bootstrap_replay,
    bootstrap_metrics,
    verdict_from_bootstrap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(idx: int, pnl: float) -> BacktestTrade:
    return BacktestTrade(
        trade_idx=idx,
        market_slug=f"m{idx}",
        side="YES_UP",
        entry_ts=1_700_000_000.0 + idx,
        entry_price=0.5,
        stake_usd=1.0,
        exit_ts=1_700_000_300.0 + idx,
        exit_price=1.0 if pnl > 0 else 0.0,
        resolution="win" if pnl > 0 else "loss",
        pnl_usd=pnl,
        slippage=0.0,
        fee=0.0,
        entry_t_in_window=150.0,
        signal_first_seen_t_in_window=150.0,
        implied_prob_yes_at_entry=0.5,
        model_prob_yes_at_entry=0.6,
        edge_at_entry=0.1,
        pm_spread_bps_at_entry=20.0,
        vol_regime_at_entry="mid",
    )


# ---------------------------------------------------------------------------
# Trade-vector bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_metrics_empty_trades_returns_zero_iter() -> None:
    r = bootstrap_metrics(trades=[], n_iter=100, seed=1)
    assert r.n_iter == 0
    assert r.realized["n_trades"] == 0
    assert r.permutation_pvalue is None


def test_bootstrap_metrics_realized_matches_manual() -> None:
    # 6 trades: +1, +1, -1, +1, -1, -1 → total 0, win_rate 0.5.
    trades = [_trade(i, p) for i, p in enumerate([1, 1, -1, 1, -1, -1])]
    r = bootstrap_metrics(trades=trades, n_iter=200, seed=7)
    assert r.realized["n_trades"] == 6
    assert r.realized["total_pnl"] == 0.0
    assert r.realized["win_rate"] == pytest.approx(0.5)
    # Bootstrap percentile distribution must bracket the realized total.
    p5 = r.percentiles["total_pnl"]["p5"]
    p95 = r.percentiles["total_pnl"]["p95"]
    assert p5 <= r.realized["total_pnl"] <= p95


def test_bootstrap_metrics_strong_winner_distribution() -> None:
    # 100 trades, all wins of +0.5. Bootstrap means stay tight near 0.5.
    trades = [_trade(i, 0.5) for i in range(100)]
    r = bootstrap_metrics(trades=trades, n_iter=500, seed=3, include_permutation=False)
    # Mean of resampled total PnL ≈ 50.0; std should be 0 (no variance).
    assert r.means["total_pnl"] == pytest.approx(50.0, abs=1e-9)
    assert r.stds["total_pnl"] == pytest.approx(0.0, abs=1e-9)
    # Permutation NaN for an all-wins vector — verify by re-running with flag.
    r2 = bootstrap_metrics(trades=trades, n_iter=200, seed=3)
    assert r2.permutation_pvalue is not None
    assert math.isnan(r2.permutation_pvalue)


def test_permutation_pvalue_low_for_clear_edge() -> None:
    # Mostly winners: 80 wins of +0.95 and 20 losses of -1.0. Mean = +0.56.
    # Coin-flip null centred at zero should almost never reach this total.
    pnls = [0.95] * 80 + [-1.0] * 20
    trades = [_trade(i, p) for i, p in enumerate(pnls)]
    r = bootstrap_metrics(trades=trades, n_iter=2000, seed=9)
    assert r.permutation_pvalue is not None
    assert r.permutation_pvalue < 0.05


def test_permutation_pvalue_high_for_pure_noise() -> None:
    # Symmetric +1/-1 alternation with 50/50 split → realized total 0.
    # Coin-flip null centred at 0 → p-value ≈ 0.5.
    pnls = [1.0, -1.0] * 50
    trades = [_trade(i, p) for i, p in enumerate(pnls)]
    r = bootstrap_metrics(trades=trades, n_iter=2000, seed=11)
    assert r.permutation_pvalue is not None
    assert 0.3 < r.permutation_pvalue < 0.7


def test_verdict_from_bootstrap_edge_likely() -> None:
    pnls = [0.95] * 80 + [-1.0] * 20
    trades = [_trade(i, p) for i, p in enumerate(pnls)]
    r = bootstrap_metrics(trades=trades, n_iter=2000, seed=4)
    assert verdict_from_bootstrap(r) == "edge_likely"


def test_verdict_from_bootstrap_no_edge_on_loser() -> None:
    pnls = [-1.0] * 80 + [0.95] * 20
    trades = [_trade(i, p) for i, p in enumerate(pnls)]
    r = bootstrap_metrics(trades=trades, n_iter=2000, seed=5)
    assert verdict_from_bootstrap(r) == "no_edge"


def test_verdict_inconclusive_on_empty() -> None:
    r = bootstrap_metrics(trades=[], n_iter=100, seed=1)
    assert verdict_from_bootstrap(r) == "inconclusive"


# ---------------------------------------------------------------------------
# Block bootstrap of market windows
# ---------------------------------------------------------------------------


class _AlwaysEnter(StrategyBase):
    name = "test_always_enter"

    def should_enter(self, ctx: TickContext) -> Decision:
        if 120.0 <= ctx.t_in_window <= 240.0:
            return Decision(action=Action.ENTER, side=Side.YES_UP, reason="test")
        return Decision(action=Action.SKIP, reason="not_in_window")


def _mk_ticks(slug: str, n: int, start_ts: float, settles_up: bool) -> list[TickContext]:
    """Synthesize ``n`` ticks at 1 Hz over a 5-minute window.

    Each market opens at price 100, closes at 101 (up) or 99 (down).
    Polymarket book is fixed mid 0.5 with 10 bps spread.
    """
    out: list[TickContext] = []
    open_price = 100.0
    final_price = 101.0 if settles_up else 99.0
    for i in range(n):
        spot = open_price + (final_price - open_price) * (i / max(n - 1, 1))
        out.append(
            TickContext(
                ts=start_ts + i,
                market_slug=slug,
                t_in_window=float(i),
                window_close_ts=start_ts + 299,
                spot_price=spot,
                chainlink_price=spot,
                open_price=open_price,
                pm_yes_bid=0.495,
                pm_yes_ask=0.505,
                pm_no_bid=0.495,
                pm_no_ask=0.505,
                pm_depth_yes=1000.0,
                pm_depth_no=1000.0,
                pm_imbalance=0.0,
                pm_spread_bps=20.0,
                implied_prob_yes=0.5,
                model_prob_yes=0.6,
                edge=0.1,
                z_score=0.0,
                vol_regime="mid",
            )
        )
    return out


class _FakeLoader:
    """In-memory loader: yields a fixed list of (slug, ticks)."""

    provides_settle_prices = False

    def __init__(self, markets: list[tuple[str, list[TickContext]]]) -> None:
        self._markets = markets

    def iter_markets(self, from_ts: float, to_ts: float):  # noqa: ARG002
        return iter(self._markets)


# RiskManager is constructed even under bypass_risk=True (never consulted).
# The constructor requires these keys; supplying a permissive default keeps
# the test focused on the MC path.
_PERMISSIVE_RISK_CFG: dict = {
    "cooldown_seconds": 0,
    "max_position_size_usd": 1_000.0,
    "daily_loss_limit_usd": 1_000_000.0,
    "daily_trade_limit": 1_000_000,
    "min_edge_bps": 0,
    "min_z_score": 0,
    "min_pm_depth_usd": 0,
    "skip_if_spread_bps": 1_000_000,
}


def test_block_bootstrap_replay_runs_and_populates_replicates() -> None:
    # 6 source markets — half settle up, half down. With YES_UP entries
    # and a 50/50 split, mean total_pnl is roughly zero.
    markets: list[tuple[str, list[TickContext]]] = []
    for i in range(6):
        markets.append(
            (f"slug_{i}", _mk_ticks(f"slug_{i}", 60, 1_700_000_000 + i * 300, i % 2 == 0))
        )
    loader = _FakeLoader(markets)

    res = block_bootstrap_replay(
        strategy_factory=lambda: _AlwaysEnter(config={"params": {}}),
        loader=loader,
        from_ts=1_700_000_000,
        to_ts=1_700_000_000 + 6 * 300,
        stake_usd=1.0,
        fill_cfg=FillConfig(slippage_bps=0.0, fill_probability=1.0),
        entry_window=EntryWindowConfig(earliest_entry_t_s=0, latest_entry_t_s=240),
        risk_cfg=_PERMISSIVE_RISK_CFG,
        config_used={},
        n_iter=20,
        seed=42,
        bypass_risk=True,
    )

    assert res.n_iter == 20
    assert res.n_source_markets == 6
    assert len(res.replicates) == 20
    # Every replicate must have produced exactly 6 markets (sampled with
    # replacement preserving size).
    assert all(r.n_markets == 6 for r in res.replicates)
    # Percentiles are present for the four headline KPIs.
    for k in ("total_pnl", "win_rate", "sharpe_per_trade", "max_drawdown_usd"):
        assert "p5" in res.percentiles[k] and "p95" in res.percentiles[k]
        assert res.percentiles[k]["p5"] <= res.percentiles[k]["p95"]


def test_block_bootstrap_replay_zero_markets() -> None:
    res = block_bootstrap_replay(
        strategy_factory=lambda: _AlwaysEnter(config={"params": {}}),
        loader=_FakeLoader([]),
        from_ts=0.0,
        to_ts=1.0,
        stake_usd=1.0,
        fill_cfg=FillConfig(slippage_bps=0.0, fill_probability=1.0),
        entry_window=EntryWindowConfig(earliest_entry_t_s=0, latest_entry_t_s=240),
        risk_cfg=_PERMISSIVE_RISK_CFG,
        config_used={},
        n_iter=5,
        seed=1,
        bypass_risk=True,
    )
    assert res.n_iter == 0
    assert res.n_source_markets == 0
    assert res.replicates == []
