"""Liquidation-cluster gravity signal (ADR 0012)."""

from __future__ import annotations

from trading.engine.features.liquidation_gravity import (
    LiqCluster,
    gravity_scores,
    signed_gravity,
)


def _cluster(side: str, price: float, size_usd: float) -> LiqCluster:
    return LiqCluster(ts=0.0, side=side, price=price, size_usd=size_usd)


def test_no_clusters_returns_zero() -> None:
    assert gravity_scores(70_000.0, []) == (0.0, 0.0)


def test_cluster_below_contributes_to_down() -> None:
    clusters = [_cluster("long", 69_800.0, 50_000.0)]
    down, up = gravity_scores(70_000.0, clusters)
    assert down > 0.0
    assert up == 0.0


def test_cluster_above_contributes_to_up() -> None:
    clusters = [_cluster("short", 70_200.0, 50_000.0)]
    down, up = gravity_scores(70_000.0, clusters)
    assert down == 0.0
    assert up > 0.0


def test_close_large_cluster_saturates() -> None:
    # Cluster within 0.15 % + size ≥ 100k → saturates to 1.0 on its side.
    clusters = [_cluster("long", 70_000.0 * (1 - 0.001), 500_000.0)]
    down, _ = gravity_scores(70_000.0, clusters)
    assert down == 1.0


def test_far_cluster_ignored() -> None:
    # > 0.30 % away → weight 0.
    clusters = [_cluster("long", 70_000.0 * (1 - 0.01), 500_000.0)]
    down, up = gravity_scores(70_000.0, clusters)
    assert down == 0.0
    assert up == 0.0


def test_signed_gravity_range() -> None:
    import pytest

    assert signed_gravity(down=0.0, up=0.0) == pytest.approx(0.0, abs=1e-9)
    assert signed_gravity(down=1.0, up=0.0) == pytest.approx(-1.0, abs=1e-9)
    assert signed_gravity(down=0.0, up=1.0) == pytest.approx(1.0, abs=1e-9)
    assert signed_gravity(down=0.3, up=0.7) == pytest.approx(0.4, abs=1e-9)


def test_signed_gravity_clamped() -> None:
    # Both > 1 input (pathological) → still bounded.
    assert -1.0 <= signed_gravity(2.0, 0.0) <= 1.0
    assert -1.0 <= signed_gravity(0.0, 2.0) <= 1.0
