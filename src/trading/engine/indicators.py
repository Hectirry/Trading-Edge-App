"""Technical indicators + Black-Scholes binary probability.

Literal port of /home/coder/polybot-btc5m/core/indicators.py. Required by the
imbalance_v3 backtest to recompute model_prob_yes / edge / z_score fresh per
market (polybot does the same — the values stored in the `ticks` table are
live-time captures and cannot be reused offline without drift).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class EMA:
    period: int
    value: float = 0.0
    initialized: bool = False

    def update(self, x: float) -> float:
        if not self.initialized:
            self.value = x
            self.initialized = True
        else:
            alpha = 2 / (self.period + 1)
            self.value = alpha * x + (1 - alpha) * self.value
        return self.value


@dataclass
class RSI:
    period: int = 14
    gains: deque = field(default_factory=lambda: deque(maxlen=500))
    losses: deque = field(default_factory=lambda: deque(maxlen=500))
    prev: float | None = None

    def update(self, x: float) -> float:
        if self.prev is None:
            self.prev = x
            return 50.0
        delta = x - self.prev
        self.gains.append(max(delta, 0))
        self.losses.append(max(-delta, 0))
        self.prev = x
        if len(self.gains) < self.period:
            return 50.0
        avg_g = sum(list(self.gains)[-self.period :]) / self.period
        avg_l = sum(list(self.losses)[-self.period :]) / self.period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))


@dataclass
class RealizedVol:
    """Rolling stdev of log-returns, annualized (365*24*3600 at 1s cadence)."""

    window_seconds: int = 60
    returns: deque = field(default_factory=lambda: deque(maxlen=3600))
    prev: float | None = None

    def update(self, x: float) -> float:
        if self.prev is not None and self.prev > 0 and x > 0:
            self.returns.append(math.log(x / self.prev))
        self.prev = x
        n = min(len(self.returns), self.window_seconds)
        if n < 2:
            return 0.0
        sample = list(self.returns)[-n:]
        mean = sum(sample) / n
        var = sum((r - mean) ** 2 for r in sample) / (n - 1)
        return math.sqrt(var * 365 * 24 * 3600)


@dataclass
class EWMAVol:
    """RiskMetrics EWMA σ, annualized."""

    lam: float = 0.94
    var: float = 0.0
    prev: float | None = None

    def update(self, x: float) -> float:
        if self.prev is None or self.prev <= 0 or x <= 0:
            self.prev = x
            return 0.0
        r = math.log(x / self.prev)
        self.var = self.lam * self.var + (1 - self.lam) * r * r
        self.prev = x
        return math.sqrt(self.var * 365 * 24 * 3600)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes_binary_prob(S: float, K: float, sigma: float, T: float) -> float:
    """P(S_T > K) under GBM without drift. sigma annualized, T in years."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.5
    d2 = (math.log(S / K) - (sigma**2) / 2 * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d2)


def rolling_zscore(values: list[float], window: int = 60) -> float:
    if len(values) < 2:
        return 0.0
    sample = values[-window:]
    n = len(sample)
    mean = sum(sample) / n
    var = sum((v - mean) ** 2 for v in sample) / max(n - 1, 1)
    std = math.sqrt(var) if var > 0 else 1e-9
    return (values[-1] - mean) / std


@dataclass
class IndicatorStack:
    """Per-market indicator state.

    Polybot recreates this per market (core/backtest.py._IndicatorStack).
    We mirror that — caller constructs a fresh IndicatorStack for each
    market_slug being replayed.
    """

    ema_fast_period: int = 12
    ema_slow_period: int = 26
    rsi_period: int = 14
    vol_window_seconds: int = 60
    vol_ewma_lambda: float = 0.94

    def __post_init__(self) -> None:
        self.ema_fast = EMA(self.ema_fast_period)
        self.ema_slow = EMA(self.ema_slow_period)
        self.rsi = RSI(self.rsi_period)
        self.vol_realized = RealizedVol(self.vol_window_seconds)
        self.vol_ewma = EWMAVol(self.vol_ewma_lambda)
        self.recent_edges: deque = deque(maxlen=120)

    def update(self, ctx) -> None:
        ctx.ema_fast = self.ema_fast.update(ctx.spot_price)
        ctx.ema_slow = self.ema_slow.update(ctx.spot_price)
        ctx.rsi_14 = self.rsi.update(ctx.spot_price)
        ctx.vol_realized = self.vol_realized.update(ctx.spot_price)
        ctx.vol_ewma = self.vol_ewma.update(ctx.spot_price)
        T_years = max(ctx.t_to_close / (365 * 24 * 3600), 1e-8)
        sigma = ctx.vol_ewma if ctx.vol_ewma > 0 else 0.80
        ctx.model_prob_yes = black_scholes_binary_prob(
            S=ctx.spot_price, K=ctx.open_price, sigma=sigma, T=T_years
        )
        ctx.edge = ctx.model_prob_yes - ctx.implied_prob_yes
        self.recent_edges.append(ctx.edge)
        ctx.z_score = rolling_zscore(list(self.recent_edges), 60)
