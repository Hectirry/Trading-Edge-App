"""Step 1 tests: Polymarket fee + maker rebate model."""

from __future__ import annotations

import pytest

from trading.strategies.polymarket_btc15m._fee_model import (
    DEFAULT_FEE_A,
    DEFAULT_FEE_B,
    MIN_PAYOUT_USDC,
    REBATE_RATE_BY_CATEGORY,
    RebateCategory,
    expected_fill_economics,
    fee_maker,
    fee_taker,
    rebate_pool_share,
)


# ─── fee_taker ───────────────────────────────────────────────────────────


def test_fee_taker_at_p_half_is_max():
    """Parabolic fee peaks at p=0.5: fee = fee_a + fee_b."""
    expected = DEFAULT_FEE_A + DEFAULT_FEE_B
    assert fee_taker(0.5) == pytest.approx(expected, abs=1e-9)


def test_fee_taker_at_extremes_is_floor():
    """At p=0 or p=1, fee = fee_a (zero parabolic contribution)."""
    assert fee_taker(0.0) == pytest.approx(DEFAULT_FEE_A, abs=1e-9)
    assert fee_taker(1.0) == pytest.approx(DEFAULT_FEE_A, abs=1e-9)


def test_fee_taker_symmetric():
    """fee(p) == fee(1-p) — parabolic in p×(1-p)."""
    for p in (0.1, 0.18, 0.3, 0.45):
        assert fee_taker(p) == pytest.approx(fee_taker(1.0 - p), abs=1e-9)


def test_fee_taker_outside_domain():
    """Defensive: returns 0 outside [0, 1]."""
    assert fee_taker(-0.1) == 0.0
    assert fee_taker(1.1) == 0.0


def test_fee_taker_custom_constants():
    """Custom fee_a/fee_b override defaults."""
    assert fee_taker(0.5, fee_a=0.0, fee_b=0.04) == pytest.approx(0.04, abs=1e-9)
    assert fee_taker(0.5, fee_a=0.01, fee_b=0.0) == pytest.approx(0.01, abs=1e-9)


def test_fee_taker_at_v1_bucket_midpoint():
    """At p=0.175 (V1 bucket 0.15-0.20 mid): fee_a + fee_b·4·0.175·0.825."""
    expected = DEFAULT_FEE_A + DEFAULT_FEE_B * 4.0 * 0.175 * 0.825
    assert fee_taker(0.175) == pytest.approx(expected, abs=1e-9)


# ─── fee_maker ───────────────────────────────────────────────────────────


def test_fee_maker_always_zero():
    """Polymarket: makers are never charged. fee_maker returns 0 always."""
    for p in (0.0, 0.1, 0.18, 0.5, 0.7, 1.0, -0.1, 1.5):
        assert fee_maker(p) == 0.0


# ─── rebate_pool_share ───────────────────────────────────────────────────


def test_rebate_pool_share_crypto_20pct():
    """Crypto category pays 20% of taker fees as maker rebate pool."""
    payout = rebate_pool_share(my_volume_share=1.0, total_taker_fees_pool=100.0, category=RebateCategory.CRYPTO)
    assert payout == pytest.approx(20.0, abs=1e-9)


def test_rebate_pool_share_proportional():
    """Payout proportional to my_volume_share."""
    full_pool = rebate_pool_share(1.0, 1000.0, RebateCategory.CRYPTO)  # 200
    half_pool = rebate_pool_share(0.5, 1000.0, RebateCategory.CRYPTO)  # 100
    quarter_pool = rebate_pool_share(0.25, 1000.0, RebateCategory.CRYPTO)  # 50
    assert full_pool == pytest.approx(200.0, abs=1e-9)
    assert half_pool == pytest.approx(100.0, abs=1e-9)
    assert quarter_pool == pytest.approx(50.0, abs=1e-9)


def test_rebate_pool_share_other_categories_25pct():
    """Sports / Finance / Politics / etc. pay 25%."""
    for cat in (
        RebateCategory.SPORTS,
        RebateCategory.FINANCE,
        RebateCategory.POLITICS,
        RebateCategory.ECONOMICS,
        RebateCategory.CULTURE,
        RebateCategory.WEATHER,
        RebateCategory.TECH,
        RebateCategory.MENTIONS,
        RebateCategory.OTHER,
    ):
        assert REBATE_RATE_BY_CATEGORY[cat] == 0.25
        assert rebate_pool_share(1.0, 100.0, cat) == pytest.approx(25.0, abs=1e-9)


def test_rebate_pool_share_geopolitics_zero():
    """Geopolitics: fee-free (zero rebate)."""
    assert rebate_pool_share(1.0, 100.0, RebateCategory.GEOPOLITICS) == 0.0


def test_rebate_pool_share_min_payout_threshold():
    """Below MIN_PAYOUT_USDC ($1), payout returns 0."""
    # 20% × $1 × 1.0 = $0.20 — below $1 threshold
    assert rebate_pool_share(1.0, 1.0, RebateCategory.CRYPTO) == 0.0
    # 20% × $5.50 × 1.0 = $1.10 — above threshold
    assert rebate_pool_share(1.0, 5.50, RebateCategory.CRYPTO) == pytest.approx(1.10, abs=1e-9)


def test_rebate_pool_share_zero_volume_returns_zero():
    assert rebate_pool_share(0.0, 100.0, RebateCategory.CRYPTO) == 0.0
    assert rebate_pool_share(1.0, 0.0, RebateCategory.CRYPTO) == 0.0


def test_rebate_pool_share_string_category():
    """String category accepted (TOML config will pass strings)."""
    assert rebate_pool_share(1.0, 100.0, "crypto") == pytest.approx(20.0, abs=1e-9)
    assert rebate_pool_share(1.0, 100.0, "geopolitics") == 0.0


def test_rebate_pool_share_unknown_category_zero():
    """Defensive: unknown category returns 0."""
    assert rebate_pool_share(1.0, 100.0, "unknown_xyz") == 0.0


def test_min_payout_constant():
    """Sanity: MIN_PAYOUT matches Polymarket docs ($1 USDC)."""
    assert MIN_PAYOUT_USDC == 1.0


# ─── expected_fill_economics ─────────────────────────────────────────────


def test_expected_fill_economics_v1_bucket_favorable():
    """V1 bucket 0.15-0.20 with adverse_signed=-0.029 (favorable to maker)."""
    out = expected_fill_economics(
        p_fair=0.18,
        delta_capt_yes_units=0.015,
        notional_usdc=22.0,
        adverse_signed_yes_units=-0.029,
    )
    # spread = 0.015 × (22/0.18) = 0.015 × 122.22 ≈ $1.83
    assert out["spread_capt_usdc"] == pytest.approx(0.015 * (22.0 / 0.18), abs=1e-3)
    # adverse signed = -0.029 × 122.22 ≈ -$3.54 (negative = favorable)
    assert out["adverse_signed_usdc"] == pytest.approx(-0.029 * (22.0 / 0.18), abs=1e-3)
    # net pre-rebate = spread - adverse = 1.83 - (-3.54) = +$5.37
    assert out["net_pre_rebate_usdc"] > 5.0


def test_expected_fill_economics_above_zone_adverse():
    """Above-zone bucket: adverse_signed positive (adverse to maker)."""
    out = expected_fill_economics(
        p_fair=0.75,
        delta_capt_yes_units=0.015,
        notional_usdc=22.0,
        adverse_signed_yes_units=+0.026,
    )
    # net = spread - adverse = positive - positive ≈ small
    assert out["adverse_signed_usdc"] > 0
    assert out["net_pre_rebate_usdc"] < out["spread_capt_usdc"]


def test_expected_fill_economics_zero_inputs():
    """Zero notional ⇒ zero economics."""
    out = expected_fill_economics(0.18, 0.015, 0.0, -0.029)
    assert out["spread_capt_usdc"] == 0.0
    assert out["net_pre_rebate_usdc"] == 0.0


def test_expected_fill_economics_invalid_p():
    """p outside (0, 1) ⇒ all zeros."""
    out = expected_fill_economics(0.0, 0.015, 22.0, -0.029)
    assert out == {
        "spread_capt_usdc": 0.0,
        "adverse_signed_usdc": 0.0,
        "fee_taker_avoided_usdc": 0.0,
        "net_pre_rebate_usdc": 0.0,
    }


def test_expected_fill_economics_taker_fee_avoided():
    """fee_taker_avoided is positive for valid p (informational line)."""
    out = expected_fill_economics(0.5, 0.015, 22.0, 0.0)
    # At p=0.5, fee = fee_a + fee_b = 0.03 → avoided = 0.03 × 22 = $0.66
    assert out["fee_taker_avoided_usdc"] == pytest.approx(0.03 * 22.0, abs=1e-3)


def test_default_fee_constants_match_oracle_lag_v1():
    """fee_a = 0.005, fee_b = 0.025 — replicated from oracle_lag_v1 TOML."""
    assert DEFAULT_FEE_A == 0.005
    assert DEFAULT_FEE_B == 0.025
