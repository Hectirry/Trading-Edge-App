"""Fractional Kelly stake sizing (ADR 0011).

Used by last_90s_forecaster_v3 after the first ``kelly_min_trades``
settled paper trades. Before that, callers stay on a fixed ``stake_usd``
so we have empirical data to back the probability estimate.

Binary payout formulation on Polymarket:

- Buy YES at ``entry_price``; YES pays $1 if outcome Up, $0 otherwise.
- Odds ratio ``b = (1 - p_market) / p_market`` where ``p_market`` is the
  entry price (implied probability).
- Kelly fraction: ``f* = max(0, (b*p - (1 - p)) / b)`` where ``p`` is the
  model's probability of the chosen side winning.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellyConfig:
    stake_min_usd: float = 5.0
    stake_max_usd: float = 15.0
    kelly_fraction: float = 0.25
    min_trades: int = 20


def kelly_fraction(p_win: float, entry_price: float) -> float:
    """``f*`` — unbounded Kelly fraction."""
    if entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0
    b = (1.0 - entry_price) / entry_price
    return max(0.0, (b * p_win - (1.0 - p_win)) / b)


def stake_for_trade(
    *,
    p_win: float,
    entry_price: float,
    capital_usd: float,
    n_settled_trades: int,
    cfg: KellyConfig,
) -> float:
    """Return the $ stake for the next trade.

    Hybrid policy: first ``cfg.min_trades`` settled paper trades use the
    floor stake (``stake_min_usd``), so the sizer has data to calibrate
    against before applying Kelly. Afterwards, Kelly-fractional is
    computed and clamped to ``[stake_min_usd, stake_max_usd]``.
    """
    if n_settled_trades < cfg.min_trades:
        return cfg.stake_min_usd
    f_star = kelly_fraction(p_win, entry_price)
    raw = capital_usd * cfg.kelly_fraction * f_star
    return max(cfg.stake_min_usd, min(cfg.stake_max_usd, raw))
