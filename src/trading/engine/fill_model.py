"""Fill model + exec sim ported from /home/coder/polybot-btc5m/core/executor.py.

Parabolic fee: fee = k * p * (1 - p) * notional. Applied both on wins and
losses. Slippage + fill probability as paper simulation; FOK semantics
mirrored (either fill whole size or skip the trade).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from trading.engine.types import Side


@dataclass
class FillParams:
    fee_k: float = 0.05
    slippage_bps: float = 10.0
    fill_probability: float = 0.95


@dataclass
class FillResult:
    filled: bool
    entry_price: float
    slippage: float
    fee: float


def deterministic_rng(seed_source: str) -> random.Random:
    """Produce a deterministic RNG per trade attempt so backtests are
    reproducible across runs (fill-probability coin-flip included)."""
    h = hashlib.sha256(seed_source.encode()).digest()
    seed = int.from_bytes(h[:8], "big")
    return random.Random(seed)


def simulate_fill(
    side: Side,
    pm_yes_ask: float,
    pm_no_ask: float,
    stake_usd: float,
    params: FillParams,
    seed_source: str,
) -> FillResult:
    """Attempt a paper fill; returns FillResult with filled=True/False.

    seed_source must be stable across runs for determinism
    (e.g., market_slug + entry_ts).
    """
    rng = deterministic_rng(seed_source)
    if rng.random() > params.fill_probability:
        return FillResult(False, 0.0, 0.0, 0.0)

    base = pm_yes_ask if side is Side.YES_UP else pm_no_ask
    if base <= 0.0 or base >= 1.0:
        return FillResult(False, 0.0, 0.0, 0.0)
    slip = base * params.slippage_bps / 10_000.0
    entry = min(base + slip, 0.99)
    fee = params.fee_k * entry * (1.0 - entry) * stake_usd
    return FillResult(True, entry, slip, fee)


def settle(
    side: Side,
    entry_price: float,
    stake_usd: float,
    fee: float,
    outcome_went_up: bool,
) -> tuple[str, float, float]:
    """Apply window-close settlement. Returns (resolution, exit_price, pnl).

    Polymarket resolves YES token at 1.0 when the market outcome is Up,
    0.0 otherwise. Shares purchased for the selected side = stake / entry_price.
    PnL = shares * exit_price - stake - fee.
    """
    if entry_price <= 0.0:
        return "error", 0.0, -stake_usd - fee
    shares = stake_usd / entry_price
    chose_up = side is Side.YES_UP
    wins = (chose_up and outcome_went_up) or (not chose_up and not outcome_went_up)
    exit_price = 1.0 if wins else 0.0
    pnl = shares * exit_price - stake_usd - fee
    return ("win" if wins else "loss"), exit_price, pnl
