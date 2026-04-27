"""Polymarket fee + maker rebate model (mm_rebate_v1 helpers).

Verified against docs.polymarket.com on 2026-04-27. Erratum to ADR 0014
which incorrectly stated "0% maker rebate"; the rebate program is in fact
ACTIVE and pays a category-specific share of taker fees collected.

Public API
----------
- ``fee_taker(p, fee_a, fee_b)`` — per unit notional, parabolic in p.
  Replicated bit-exact from oracle_lag_v1's TOML constants and the
  Polymarket dynamic-fee formula.
- ``fee_maker(p)`` — always 0.0. Makers are never charged fees per
  https://docs.polymarket.com/trading/fees.md.
- ``rebate_pool_share(my_volume_share, total_taker_fees_pool, category)``
  — proportional rebate, computed at the bucket / market level (not
  per-fill).
- ``RebateCategory`` constants — Crypto 20%, others 25%, Geopolitics 0%.

Step 0 v2 dependencies (TODO)
-----------------------------
- ``rebate_pool_share`` currently treats category rate as fixed; if
  Polymarket's rate schedule changes (per ``getCurrentRebatedFeesForAMaker``
  endpoint), the constants here become stale. A thin live-rate fetcher in
  Step 2 will validate and update.
- ``fee_taker`` parameters fee_a/fee_b are inherited from oracle_lag_v1
  but Polymarket's published formula uses ``feeRate × p × (1-p)`` with a
  category-specific feeRate. Cross-validate against the Polymarket docs
  during Step 2 backtest setup.
"""

from __future__ import annotations

from enum import Enum


# Default parabolic-fee constants from oracle_lag_v1 TOML; replicate exactly
# so taker fee accounting is consistent across strategies.
DEFAULT_FEE_A = 0.005
DEFAULT_FEE_B = 0.025


class RebateCategory(str, Enum):
    """Polymarket rebate category — controls the % of taker fees redistributed
    to makers. Per docs.polymarket.com/market-makers/maker-rebates.md.
    """

    CRYPTO = "crypto"
    SPORTS = "sports"
    FINANCE = "finance"
    POLITICS = "politics"
    ECONOMICS = "economics"
    CULTURE = "culture"
    WEATHER = "weather"
    TECH = "tech"
    MENTIONS = "mentions"
    OTHER = "other"
    GEOPOLITICS = "geopolitics"  # 0% — fee-free


# Per docs.polymarket.com/market-makers/maker-rebates.md, accessed 2026-04-27.
# Pool size is (rebate_rate * taker_fees_collected_in_market_in_period).
REBATE_RATE_BY_CATEGORY: dict[RebateCategory, float] = {
    RebateCategory.CRYPTO: 0.20,
    RebateCategory.SPORTS: 0.25,
    RebateCategory.FINANCE: 0.25,
    RebateCategory.POLITICS: 0.25,
    RebateCategory.ECONOMICS: 0.25,
    RebateCategory.CULTURE: 0.25,
    RebateCategory.WEATHER: 0.25,
    RebateCategory.TECH: 0.25,
    RebateCategory.MENTIONS: 0.25,
    RebateCategory.OTHER: 0.25,
    RebateCategory.GEOPOLITICS: 0.0,
}

# Minimum payout threshold per Polymarket docs.
MIN_PAYOUT_USDC = 1.0


def fee_taker(p: float, fee_a: float = DEFAULT_FEE_A, fee_b: float = DEFAULT_FEE_B) -> float:
    """Polymarket dynamic taker fee per unit notional.

    Formula: fee(p) = fee_a + fee_b · 4 · p · (1 − p)

    Domain: p ∈ [0, 1]. At extremes the fee is fee_a; at p=0.5 it is
    fee_a + fee_b. Returns 0.0 outside the legal domain (defensive).
    """
    if not (0.0 <= p <= 1.0):
        return 0.0
    return fee_a + fee_b * 4.0 * p * (1.0 - p)


def fee_maker(p: float) -> float:
    """Polymarket maker side: never charged fees.

    Returns 0 unconditionally. Makers earn a rebate instead, computed at
    the bucket/market level via ``rebate_pool_share``.

    The argument ``p`` is accepted (and ignored) so callers can swap
    fee_taker / fee_maker behind the same signature.
    """
    return 0.0


def rebate_pool_share(
    my_volume_share: float,
    total_taker_fees_pool: float,
    category: RebateCategory | str = RebateCategory.CRYPTO,
) -> float:
    """Maker's share of the rebate pool for a market / period.

    Polymarket pays a category-specific percentage of taker fees collected
    in the market, distributed proportionally to makers by their share of
    filled maker volume.

    Args:
      my_volume_share: ∈ [0, 1] — fraction of total maker volume in the
        market provided by us.
      total_taker_fees_pool: total taker fees collected in the market over
        the rebate accrual period (USDC).
      category: market category — "crypto" by default for BTC up/down.

    Returns: USDC amount the maker is entitled to. Returns 0 if below the
    minimum payout threshold.
    """
    if my_volume_share <= 0 or total_taker_fees_pool <= 0:
        return 0.0
    if isinstance(category, str):
        try:
            category = RebateCategory(category)
        except ValueError:
            return 0.0
    rate = REBATE_RATE_BY_CATEGORY.get(category, 0.0)
    if rate <= 0:
        return 0.0
    pool = rate * total_taker_fees_pool
    payout = pool * my_volume_share
    if payout < MIN_PAYOUT_USDC:
        return 0.0
    return payout


def expected_fill_economics(
    p_fair: float,
    delta_capt_yes_units: float,
    notional_usdc: float,
    adverse_signed_yes_units: float,
    fee_a: float = DEFAULT_FEE_A,
    fee_b: float = DEFAULT_FEE_B,
) -> dict[str, float]:
    """Per-fill economic decomposition for tabletop reasoning.

    Computes:
      - spread captured (always positive USDC) = δ_capt × shares
      - adverse cost (signed; negative = favorable to maker) =
          adverse_signed × shares
      - taker fee avoided = fee_taker(p) × notional (informational; the
          maker doesn't pay this, so it's a "savings" line)
      - net per fill (excluding rebate, which is computed at pool level)

    Returns a dict with all components in USDC.

    Note: rebate is NOT per-fill; it's computed via ``rebate_pool_share``
    over an accrual period. This function quantifies the per-fill side of
    the economics.
    """
    if not (0.0 < p_fair < 1.0) or notional_usdc <= 0:
        return {
            "spread_capt_usdc": 0.0,
            "adverse_signed_usdc": 0.0,
            "fee_taker_avoided_usdc": 0.0,
            "net_pre_rebate_usdc": 0.0,
        }
    shares = notional_usdc / p_fair
    spread_capt = delta_capt_yes_units * shares
    adverse = adverse_signed_yes_units * shares
    fee_avoided = fee_taker(p_fair, fee_a, fee_b) * notional_usdc
    net = spread_capt - adverse  # adverse is signed; negative ⇒ adds to net
    return {
        "spread_capt_usdc": spread_capt,
        "adverse_signed_usdc": adverse,
        "fee_taker_avoided_usdc": fee_avoided,
        "net_pre_rebate_usdc": net,
    }
