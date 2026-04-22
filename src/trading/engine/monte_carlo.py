"""Monte Carlo bootstrap — literal port of
/home/coder/BTC-Tendencia-5m/core/monte_carlo.py.

Used by `trend_confirm_t1_v1` for a shadow-mode probability estimate
(P(close > strike)). In the default config the result is logged but NOT
counted toward min_confirmations (mc_shadow=True), so it does not affect
the decision vector. Kept for parity of `signal_features["mc_prob_up"]`.
"""

from __future__ import annotations

import numpy as np


def mc_bootstrap_prob_up(
    spot_series: list[float],
    current_price: float,
    strike: float,
    horizon_s: int,
    n_sims: int = 1000,
    min_returns: int = 30,
    seed: int | None = None,
) -> float:
    if len(spot_series) < min_returns + 1 or current_price <= 0 or strike <= 0:
        return 0.5
    arr = np.asarray(spot_series, dtype=float)
    arr = arr[arr > 0]
    if len(arr) < min_returns + 1:
        return 0.5
    returns = np.diff(np.log(arr))
    if len(returns) < min_returns:
        return 0.5
    rng = np.random.default_rng(seed)
    sampled = rng.choice(returns, size=(n_sims, horizon_s), replace=True)
    log_final = np.log(current_price) + sampled.sum(axis=1)
    final_prices = np.exp(log_final)
    return float((final_prices > strike).mean())
