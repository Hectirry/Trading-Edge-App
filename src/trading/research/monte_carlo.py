"""Monte Carlo evaluation of completed backtests.

Two flavors here, both keyed off `BacktestRunResult`:

(1) ``bootstrap_metrics`` / ``permutation_pvalue`` — operate on the
    realized trade vector. Cheap. Answer "how tight was this PnL?"
    and "is the win rate distinguishable from a coin flip?".

(2) ``block_bootstrap_replay`` — resamples 5-minute Polymarket market
    windows with replacement and re-runs the same driver against each
    replicate. Expensive (O(n_iter × backtest_runtime)) but answers
    "does the strategy depend on the specific set of windows?".

Distinct from ``trading.engine.monte_carlo`` (in-strategy bootstrap of
spot prices for the ``mc_prob_up`` confirmation gate, used by
``trend_confirm_t1_v1``). That module is unrelated; do not merge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from trading.engine.backtest_driver import (
    BacktestRunResult,
    BacktestTrade,
    EntryWindowConfig,
    FillConfig,
    IndicatorConfig,
    run_backtest,
)
from trading.engine.risk import RiskManager
from trading.engine.types import TickContext

PERCENTILES: tuple[int, ...] = (5, 25, 50, 75, 95)


@dataclass
class TradeBootstrapResult:
    n_iter: int
    seed: int
    realized: dict[str, float]
    percentiles: dict[str, dict[str, float]]
    means: dict[str, float]
    stds: dict[str, float]
    permutation_pvalue: float | None = None
    generated_at: str = ""


@dataclass
class BlockBootstrapReplicate:
    iter_idx: int
    n_markets: int
    n_trades: int
    total_pnl: float
    win_rate: float
    sharpe_per_trade: float
    max_drawdown_usd: float


@dataclass
class BlockBootstrapResult:
    n_iter: int
    seed: int
    n_source_markets: int
    realized: dict[str, float]
    percentiles: dict[str, dict[str, float]]
    means: dict[str, float]
    stds: dict[str, float]
    replicates: list[BlockBootstrapReplicate] = field(default_factory=list)
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Trade-vector statistics
# ---------------------------------------------------------------------------


def _summary_from_pnls(pnls: np.ndarray) -> dict[str, float]:
    n = pnls.size
    if n == 0:
        return {
            "n_trades": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "sharpe_per_trade": 0.0,
            "max_drawdown_usd": 0.0,
        }
    wins = int((pnls > 0).sum())
    mean = float(pnls.mean())
    std = float(pnls.std(ddof=1)) if n > 1 else 0.0
    sharpe = mean / std if std > 0 else 0.0
    equity = pnls.cumsum()
    peak = np.maximum.accumulate(equity)
    dd = float((equity - peak).min()) if n else 0.0
    return {
        "n_trades": n,
        "total_pnl": float(pnls.sum()),
        "win_rate": wins / n,
        "sharpe_per_trade": sharpe,
        "max_drawdown_usd": dd,
    }


def _percentile_dict(matrix: dict[str, np.ndarray]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for k, arr in matrix.items():
        out[k] = {f"p{p}": float(np.percentile(arr, p)) for p in PERCENTILES}
    return out


def bootstrap_metrics(
    trades: list[BacktestTrade],
    n_iter: int = 1000,
    seed: int = 42,
    include_permutation: bool = True,
) -> TradeBootstrapResult:
    """Resample the realized trade vector with replacement.

    Returns percentiles for total_pnl, win_rate, sharpe_per_trade,
    max_drawdown_usd. ``include_permutation`` runs a coin-flip null
    over wins/losses using the realized win rate as the alternative.
    """
    pnls = np.array([t.pnl_usd for t in trades], dtype=float)
    n = pnls.size
    realized = _summary_from_pnls(pnls)
    if n == 0:
        return TradeBootstrapResult(
            n_iter=0,
            seed=seed,
            realized=realized,
            percentiles={},
            means={},
            stds={},
            permutation_pvalue=None,
            generated_at=datetime.now(tz=UTC).isoformat(),
        )

    rng = np.random.default_rng(seed)
    # Single-shot resample matrix: (n_iter, n) — memory ~8·n_iter·n bytes.
    # n_iter=1000 × n=1000 = 8 MB; fine.
    idx = rng.integers(0, n, size=(n_iter, n))
    samples = pnls[idx]

    totals = samples.sum(axis=1)
    wins = (samples > 0).sum(axis=1) / n
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    sharpes = np.divide(means, stds, out=np.zeros_like(means), where=stds > 0)

    equity = samples.cumsum(axis=1)
    peaks = np.maximum.accumulate(equity, axis=1)
    dd = (equity - peaks).min(axis=1)

    matrices = {
        "total_pnl": totals,
        "win_rate": wins,
        "sharpe_per_trade": sharpes,
        "max_drawdown_usd": dd,
    }

    pvalue: float | None = None
    if include_permutation:
        pvalue = _permutation_pvalue_arr(pnls, n_iter=n_iter, seed=seed)

    return TradeBootstrapResult(
        n_iter=n_iter,
        seed=seed,
        realized=realized,
        percentiles=_percentile_dict(matrices),
        means={k: float(v.mean()) for k, v in matrices.items()},
        stds={k: float(v.std(ddof=1)) for k, v in matrices.items()},
        permutation_pvalue=pvalue,
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


def _permutation_pvalue_arr(pnls: np.ndarray, n_iter: int, seed: int) -> float:
    """Coin-flip null over wins/losses.

    Under H0, each trade wins with p=0.5. Outcome is binary: win → |max
    PnL across vector|, loss → -stake. We approximate by drawing wins
    from the realized |win pnl| pool and losses from the realized loss
    pool with replacement, weighting both pools at p=0.5. Returns the
    fraction of null replicates whose total PnL ≥ realized total PnL.

    For the all-wins or all-losses degenerate case the test is
    meaningless; we return NaN.
    """
    wins_pnl = pnls[pnls > 0]
    losses_pnl = pnls[pnls <= 0]
    if wins_pnl.size == 0 or losses_pnl.size == 0:
        return float("nan")
    realized_total = float(pnls.sum())
    n = pnls.size
    rng = np.random.default_rng(seed + 1)
    # Each replicate: sample n outcomes Bernoulli(0.5); for win pick a
    # random win pnl, for loss pick a random loss pnl.
    coin = rng.integers(0, 2, size=(n_iter, n))
    win_idx = rng.integers(0, wins_pnl.size, size=(n_iter, n))
    loss_idx = rng.integers(0, losses_pnl.size, size=(n_iter, n))
    null_pnls = np.where(coin == 1, wins_pnl[win_idx], losses_pnl[loss_idx])
    null_totals = null_pnls.sum(axis=1)
    return float((null_totals >= realized_total).mean())


# ---------------------------------------------------------------------------
# Block bootstrap of market windows
# ---------------------------------------------------------------------------


class _MaterializedLoader:
    """Loader shim for MC replicates.

    Wraps a fixed list of (slug, ticks) pairs and an optional settle dict.
    Honors the same ``provides_settle_prices`` capability the driver checks
    in `engine/backtest_driver.py:152`.
    """

    def __init__(
        self,
        markets: list[tuple[str, list[TickContext]]],
        settle_prices: dict[str, float] | None,
    ) -> None:
        self._markets = markets
        self._settle = settle_prices
        self.provides_settle_prices = settle_prices is not None

    def iter_markets(self, from_ts: float, to_ts: float):  # noqa: ARG002
        # MC replicates ignore the (from_ts, to_ts) window — markets are
        # already the bootstrap sample.
        return iter(self._markets)

    def market_outcomes(self, from_ts: float, to_ts: float) -> dict[str, float]:  # noqa: ARG002
        return dict(self._settle) if self._settle else {}


def _materialize(
    loader,
    from_ts: float,
    to_ts: float,
) -> tuple[list[tuple[str, list[TickContext]]], dict[str, float] | None]:
    markets = [(slug, list(ticks)) for slug, ticks in loader.iter_markets(from_ts, to_ts)]
    settle = None
    if getattr(loader, "provides_settle_prices", False):
        settle = dict(loader.market_outcomes(from_ts, to_ts))
    return markets, settle


def block_bootstrap_replay(
    *,
    strategy_factory,
    loader,
    from_ts: float,
    to_ts: float,
    stake_usd: float,
    fill_cfg: FillConfig,
    entry_window: EntryWindowConfig,
    risk_cfg: dict,
    config_used: dict,
    indicator_cfg: IndicatorConfig | None = None,
    n_iter: int = 100,
    seed: int = 42,
    bypass_risk: bool = False,
    realized: BacktestRunResult | None = None,
) -> BlockBootstrapResult:
    """Re-run the backtest driver against bootstrap-resampled markets.

    Materializes the source markets ONCE, then for each of n_iter
    replicates samples that list with replacement (preserving size) and
    invokes the same `run_backtest` path. Strategy state is reset per
    replicate via ``strategy_factory()``.
    """
    markets, settle = _materialize(loader, from_ts, to_ts)
    n_markets = len(markets)
    if n_markets == 0:
        return BlockBootstrapResult(
            n_iter=0,
            seed=seed,
            n_source_markets=0,
            realized={},
            percentiles={},
            means={},
            stds={},
            generated_at=datetime.now(tz=UTC).isoformat(),
        )

    rng = np.random.default_rng(seed)

    realized_summary: dict[str, float]
    if realized is not None:
        pnls_real = np.array([t.pnl_usd for t in realized.trades], dtype=float)
        realized_summary = {
            **_summary_from_pnls(pnls_real),
            "n_markets": realized.n_markets,
        }
    else:
        realized_summary = {}

    replicates: list[BlockBootstrapReplicate] = []
    for it in range(n_iter):
        idxs = rng.integers(0, n_markets, size=n_markets)
        sampled = [markets[i] for i in idxs]
        wrapped = _MaterializedLoader(markets=sampled, settle_prices=settle)

        result = run_backtest(
            strategy=strategy_factory(),
            loader=wrapped,
            from_ts=from_ts,
            to_ts=to_ts,
            stake_usd=stake_usd,
            fill_cfg=fill_cfg,
            entry_window=entry_window,
            risk_manager=RiskManager({"risk": risk_cfg}),
            config_used=config_used,
            indicator_cfg=indicator_cfg,
            seed=seed + it,
            bypass_risk=bypass_risk,
        )
        pnls = np.array([t.pnl_usd for t in result.trades], dtype=float)
        s = _summary_from_pnls(pnls)
        replicates.append(
            BlockBootstrapReplicate(
                iter_idx=it,
                n_markets=result.n_markets,
                n_trades=result.n_trades,
                total_pnl=s["total_pnl"],
                win_rate=s["win_rate"],
                sharpe_per_trade=s["sharpe_per_trade"],
                max_drawdown_usd=s["max_drawdown_usd"],
            )
        )

    matrices = {
        "total_pnl": np.array([r.total_pnl for r in replicates]),
        "win_rate": np.array([r.win_rate for r in replicates]),
        "sharpe_per_trade": np.array([r.sharpe_per_trade for r in replicates]),
        "max_drawdown_usd": np.array([r.max_drawdown_usd for r in replicates]),
    }

    return BlockBootstrapResult(
        n_iter=n_iter,
        seed=seed,
        n_source_markets=n_markets,
        realized=realized_summary,
        percentiles=_percentile_dict(matrices),
        means={k: float(v.mean()) for k, v in matrices.items()},
        stds={k: float(v.std(ddof=1)) if v.size > 1 else 0.0 for k, v in matrices.items()},
        replicates=replicates,
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _none_or_finite(x: float | None) -> float | None:
    if x is None:
        return None
    if not math.isfinite(x):
        return None
    return float(x)


def trade_bootstrap_to_dict(r: TradeBootstrapResult) -> dict:
    return {
        "kind": "bootstrap",
        "n_iter": r.n_iter,
        "seed": r.seed,
        "realized": r.realized,
        "percentiles": r.percentiles,
        "means": r.means,
        "stds": r.stds,
        "permutation_pvalue": _none_or_finite(r.permutation_pvalue),
        "generated_at": r.generated_at,
    }


def block_bootstrap_to_dict(r: BlockBootstrapResult) -> dict:
    return {
        "kind": "block",
        "n_iter": r.n_iter,
        "seed": r.seed,
        "n_source_markets": r.n_source_markets,
        "realized": r.realized,
        "percentiles": r.percentiles,
        "means": r.means,
        "stds": r.stds,
        "replicates": [vars(rep) for rep in r.replicates],
        "generated_at": r.generated_at,
    }


def verdict_from_bootstrap(r: TradeBootstrapResult) -> str:
    """Heuristic verdict.

    - ``no_edge``      — p5 of total_pnl ≤ 0 OR permutation p-value ≥ 0.10.
    - ``edge_likely``  — p5 of total_pnl > 0 AND permutation p-value < 0.05.
    - ``inconclusive`` — anything in between (or insufficient sample).
    """
    if r.n_iter == 0:
        return "inconclusive"
    p5 = r.percentiles.get("total_pnl", {}).get("p5", 0.0)
    pv = r.permutation_pvalue
    if p5 > 0 and pv is not None and pv < 0.05:
        return "edge_likely"
    if p5 <= 0 or (pv is not None and pv >= 0.10):
        return "no_edge"
    return "inconclusive"


__all__ = [
    "BlockBootstrapReplicate",
    "BlockBootstrapResult",
    "PERCENTILES",
    "TradeBootstrapResult",
    "block_bootstrap_replay",
    "block_bootstrap_to_dict",
    "bootstrap_metrics",
    "trade_bootstrap_to_dict",
    "verdict_from_bootstrap",
]
