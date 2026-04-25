"""Deterministic backtest driver — per-market replay with shared RiskManager.
Mirrors the shape of /home/coder/polybot-btc5m/core/backtest.py.replay_market.
See ADR 0006 for the Nautilus deferral.

PnL follows polybot backtest convention: `shares = stake/entry_price`,
`pnl = shares * 1.0 - stake` on a win, `-stake` on a loss. No fee is
applied in backtest; fee appears only in paper/live via the executor.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

from trading.common.logging import get_logger
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.indicators import IndicatorStack
from trading.engine.risk import RiskManager
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, Side, TickContext

log = get_logger(__name__)


@dataclass
class BacktestTrade:
    trade_idx: int
    market_slug: str
    side: str
    entry_ts: float
    entry_price: float
    stake_usd: float
    exit_ts: float
    exit_price: float
    resolution: str
    pnl_usd: float
    slippage: float
    fee: float
    entry_t_in_window: float
    signal_first_seen_t_in_window: float
    implied_prob_yes_at_entry: float
    model_prob_yes_at_entry: float
    edge_at_entry: float
    pm_spread_bps_at_entry: float
    vol_regime_at_entry: str
    signal_features: dict = field(default_factory=dict)
    signal_breakdown: dict = field(default_factory=dict)


@dataclass
class BacktestRunResult:
    strategy: str
    start_ts: float
    end_ts: float
    n_markets: int
    n_ticks: int
    n_trades: int
    trades: list[BacktestTrade]
    config_used: dict
    decision_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class EntryWindowConfig:
    earliest_entry_t_s: int = 120
    latest_entry_t_s: int = 240


@dataclass
class FillConfig:
    slippage_bps: float = 10.0
    fill_probability: float = 0.95
    # polybot-btc5m backtest omits the parabolic fee; polybot-agent applies
    # it. Strategies toggle per their source convention.
    apply_fee_in_backtest: bool = False
    fee_k: float = 0.05


@dataclass
class IndicatorConfig:
    ema_fast_period: int = 12
    ema_slow_period: int = 26
    rsi_period: int = 14
    vol_window_seconds: int = 60
    vol_ewma_lambda: float = 0.94


def _vol_regime_from(ctx: TickContext) -> str:
    z = abs(ctx.z_score or 0.0)
    if z < 0.5:
        return "low"
    if z < 1.5:
        return "mid"
    return "high"


def _final_price_of(ticks: list[TickContext]) -> float:
    last = ticks[-1]
    if last.chainlink_price:
        return float(last.chainlink_price)
    return float(last.spot_price or 0.0)


def _won_market(open_price: float, final_price: float, side: Side) -> bool:
    if open_price <= 0 or final_price <= 0:
        return False
    went_up = final_price > open_price
    return went_up if side is Side.YES_UP else not went_up


def _resolve_pnl(entry_price: float, stake_usd: float, won: bool) -> float:
    if entry_price <= 0:
        return -stake_usd
    shares = stake_usd / entry_price
    return shares * 1.0 - stake_usd if won else -stake_usd


def run_backtest(
    strategy: StrategyBase,
    loader: PolybotSQLiteLoader,
    from_ts: float,
    to_ts: float,
    stake_usd: float,
    fill_cfg: FillConfig,
    entry_window: EntryWindowConfig,
    risk_manager: RiskManager,
    config_used: dict,
    indicator_cfg: IndicatorConfig | None = None,
    seed: int = 42,
    bypass_risk: bool = False,
) -> BacktestRunResult:
    strategy.on_start()
    rng = random.Random(seed)
    ind_cfg = indicator_cfg or IndicatorConfig()

    trades: list[BacktestTrade] = []
    trade_idx = 0
    n_ticks = 0
    n_markets = 0
    decision_counts: dict[str, int] = {}

    # Settle dispatch: loaders that explicitly opt-in via the
    # ``provides_settle_prices`` capability supply a canonical settle dict
    # (see PaperTicksLoader.market_outcomes). Polybot does not set that
    # marker, so it keeps the legacy `_final_price_of(ticks)` path.
    # Forensic context: paper_ticks ships a chainlink_price that freezes
    # for hours on Polygon EAC, which made the legacy path settle ~64%
    # of trades wrong. See _forensics_trend_confirm_t1_v1.md.
    settle_prices: dict[str, float] | None = None
    if getattr(loader, "provides_settle_prices", False):
        settle_prices = loader.market_outcomes(from_ts, to_ts)
        log.info(
            "backtest.settle_source",
            source="loader.market_outcomes",
            n_settles=len(settle_prices),
        )
    else:
        log.info("backtest.settle_source", source="last_tick.chainlink_or_spot")

    for slug, ticks in loader.iter_markets(from_ts, to_ts):
        if not ticks:
            continue
        n_markets += 1

        # Use the loader-supplied window_close_ts (honors slug-encoding
        # convention: polybot-btc5m=close, BTC-Tendencia-5m=open+300).
        close_ts_market = ticks[0].window_close_ts or ticks[-1].ts

        # Market-opening reference price — first non-null open_price in ticks.
        open_price_market = 0.0
        for t in ticks:
            if t.open_price:
                open_price_market = float(t.open_price)
                break
        if open_price_market == 0.0:
            open_price_market = float(ticks[0].spot_price or 0.0)
        if settle_prices is not None:
            # Loader provides canonical settle prices; trust it. A missing
            # slug means the OHLCV anchor for this market wasn't ingested
            # — skip the market rather than falling back to a stale
            # chainlink reading (the bug that produced the 9.7% win-rate
            # FAIL on 2026-04-23). The fallback to chainlink is
            # intentionally NOT attempted here.
            settle = settle_prices.get(slug)
            if settle is None:
                log.warning(
                    "backtest.settle_missing",
                    slug=slug,
                    reason="no canonical settle price; market skipped",
                )
                continue
            final_price = settle
        else:
            final_price = _final_price_of(ticks)

        position: dict | None = None
        signal_first_seen: float | None = None
        recent_ctxs: list[TickContext] = []
        indicators = IndicatorStack(
            ema_fast_period=ind_cfg.ema_fast_period,
            ema_slow_period=ind_cfg.ema_slow_period,
            rsi_period=ind_cfg.rsi_period,
            vol_window_seconds=ind_cfg.vol_window_seconds,
            vol_ewma_lambda=ind_cfg.vol_ewma_lambda,
        )

        for ctx in ticks:
            n_ticks += 1
            # Recompute derived indicators fresh per market — matches
            # polybot core/backtest.py._IndicatorStack.update. Overwrites
            # ctx.edge, ctx.model_prob_yes, ctx.z_score, etc.
            indicators.update(ctx)
            # Pass ALL prior ticks from this market, not a bounded slice.
            # polybot-agent backtest_engine.replay_window does the same
            # (snapshots[:i]). trend_confirm_t1_v1's AFML features need
            # up to cusum_lookback=120 spot prices; a 30-tick slice would
            # zero them out and change the decision vector. Strategies that
            # only want a recent slice apply `[-30:]` inline, so passing
            # the full list is backward-compatible.
            ctx.recent_ticks = list(recent_ctxs)
            recent_ctxs.append(ctx)

            if position is not None:
                # Single-position-per-market; skip evaluation once entered.
                continue
            if not (
                entry_window.earliest_entry_t_s <= ctx.t_in_window <= entry_window.latest_entry_t_s
            ):
                continue

            # Risk gate first — mirrors polybot precedence. `bypass_risk`
            # is set for strategies whose live counterpart runs its own
            # risk/gate logic and whose backtest engine skips the manager
            # entirely (see trend_confirm_t1_v1 TOML:
            # `[risk].bypass_in_backtest=true`).
            if not bypass_risk:
                allowed, reason = risk_manager.can_enter(ctx)
                if not allowed:
                    decision_counts["RISK_SKIP"] = decision_counts.get("RISK_SKIP", 0) + 1
                    continue

            decision: Decision = strategy.should_enter(ctx)
            decision_counts[decision.action.value] = (
                decision_counts.get(decision.action.value, 0) + 1
            )

            if decision.action is Action.ENTER and signal_first_seen is None:
                signal_first_seen = ctx.t_in_window

            if decision.action is not Action.ENTER:
                continue

            # Fill simulation — single shared RNG.
            if rng.random() > fill_cfg.fill_probability:
                decision_counts["FILL_MISS"] = decision_counts.get("FILL_MISS", 0) + 1
                continue

            if decision.side is Side.YES_UP:
                mid = (ctx.pm_yes_bid + ctx.pm_yes_ask) / 2
                limit = ctx.pm_yes_ask
            else:
                mid = (ctx.pm_no_bid + ctx.pm_no_ask) / 2
                limit = ctx.pm_no_ask
            slip = mid * fill_cfg.slippage_bps / 10_000.0
            fill_price = min(limit + slip, 0.99)
            if fill_price <= 0:
                continue
            position = {
                "side": decision.side,
                "entry_ts": ctx.ts,
                "entry_price": fill_price,
                "slippage": slip,
                "t_in_window": ctx.t_in_window,
                "implied_prob_yes": ctx.implied_prob_yes,
                "model_prob_yes": ctx.model_prob_yes,
                "edge": ctx.edge,
                "spread_bps": ctx.pm_spread_bps,
                "vol_regime": _vol_regime_from(ctx),
                "signal_features": dict(decision.signal_features or {}),
                "signal_breakdown": dict(decision.signal_breakdown or {}),
            }

        if position is not None:
            won = _won_market(open_price_market, final_price, position["side"])
            gross_pnl = _resolve_pnl(position["entry_price"], stake_usd, won)
            fee_charged = 0.0
            if fill_cfg.apply_fee_in_backtest and position["entry_price"] > 0:
                p = position["entry_price"]
                fee_charged = fill_cfg.fee_k * p * (1.0 - p) * stake_usd
            pnl = gross_pnl - fee_charged
            resolution = "win" if won else "loss"
            trades.append(
                BacktestTrade(
                    trade_idx=trade_idx,
                    market_slug=slug,
                    side=position["side"].value,
                    entry_ts=position["entry_ts"],
                    entry_price=position["entry_price"],
                    stake_usd=stake_usd,
                    exit_ts=close_ts_market,
                    exit_price=1.0 if won else 0.0,
                    resolution=resolution,
                    pnl_usd=pnl,
                    slippage=position["slippage"],
                    fee=fee_charged,
                    entry_t_in_window=position["t_in_window"],
                    signal_first_seen_t_in_window=signal_first_seen or position["t_in_window"],
                    implied_prob_yes_at_entry=position["implied_prob_yes"],
                    model_prob_yes_at_entry=position["model_prob_yes"],
                    edge_at_entry=position["edge"],
                    pm_spread_bps_at_entry=position["spread_bps"],
                    vol_regime_at_entry=position["vol_regime"],
                    signal_features=position["signal_features"],
                    signal_breakdown=position["signal_breakdown"],
                )
            )
            trade_idx += 1
            if not bypass_risk:
                risk_manager.on_trade_closed(pnl, now=close_ts_market)
            # NOTE on parity: polybot's backtest does NOT call
            # strategy.on_trade_resolved (see core/backtest.py). Streak-based
            # pauses that fire in live therefore never fire in polybot
            # backtests. We mirror that behavior here for bit-exact parity
            # with the reference JSON.

    strategy.on_stop()

    return BacktestRunResult(
        strategy=strategy.name,
        start_ts=from_ts,
        end_ts=to_ts,
        n_markets=n_markets,
        n_ticks=n_ticks,
        n_trades=len(trades),
        trades=trades,
        config_used=config_used,
        decision_counts=decision_counts,
    )


def compute_kpis(trades: list[BacktestTrade], duration_s: float) -> dict:
    if not trades:
        return {
            "performance": {
                "n_trades": 0,
                "total_pnl": 0.0,
                "win_rate": 0.0,
                "wins": 0,
                "losses": 0,
                "avg_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
            },
            "risk_adjusted": {
                "sharpe_per_trade": 0.0,
                "sharpe_annualized_iid": 0.0,
                "sharpe_daily": 0.0,
                "mdd_usd": 0.0,
                "trades_per_year_assumed": 0.0,
            },
        }
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    n = len(trades)
    mean = total / n
    var = sum((p - mean) ** 2 for p in pnls) / max(n - 1, 1)
    std = math.sqrt(var) if var > 0 else 0.0
    sharpe_per_trade = mean / std if std > 0 else 0.0

    trades_per_year = n / max(duration_s, 1.0) * 365.25 * 86400
    sharpe_annualized_iid = sharpe_per_trade * math.sqrt(max(trades_per_year, 1.0))

    daily: dict[str, float] = {}
    for t in trades:
        day = datetime.fromtimestamp(t.entry_ts, tz=UTC).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + t.pnl_usd
    daily_pnls = list(daily.values())
    if len(daily_pnls) >= 2:
        d_mean = sum(daily_pnls) / len(daily_pnls)
        d_var = sum((x - d_mean) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
        d_std = math.sqrt(d_var) if d_var > 0 else 0.0
        sharpe_daily = (d_mean / d_std * math.sqrt(365)) if d_std > 0 else 0.0
    else:
        sharpe_daily = 0.0

    equity = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)

    return {
        "performance": {
            "n_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / n if n else 0.0,
            "total_pnl": total,
            "avg_pnl": mean,
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) != 0 else 0.0,
        },
        "risk_adjusted": {
            "sharpe_per_trade": sharpe_per_trade,
            "sharpe_annualized_iid": sharpe_annualized_iid,
            "sharpe_daily": sharpe_daily,
            "mdd_usd": mdd,
            "trades_per_year_assumed": trades_per_year,
        },
    }
