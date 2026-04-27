"""Step 1 tests: rolling k(δ) estimator. DB methods are mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from trading.strategies.polymarket_btc15m._k_estimator import KCounter, KEstimator


def test_k_initial_zero():
    e = KEstimator("mm_rebate_v1")
    assert e.k("0.15-0.20", 2) == 0.0


def test_record_fill_increments_count():
    e = KEstimator("mm_rebate_v1")
    e.record_quoting_minute("0.15-0.20", 2, period_minutes=10)
    e.record_fill("0.15-0.20", 2)
    e.record_fill("0.15-0.20", 2)
    assert e.k("0.15-0.20", 2) == 0.2  # 2 fills / 10 min


def test_record_fill_unknown_delta_ignored():
    """δ not in deltas_cents is dropped (defensive)."""
    e = KEstimator("mm_rebate_v1", deltas_cents=(1, 2, 3, 5))
    e.record_quoting_minute("0.15-0.20", 4, period_minutes=10)  # 4 not in tuple
    e.record_fill("0.15-0.20", 4)
    assert e.k("0.15-0.20", 4) == 0.0


def test_warm_start_seeds_counter():
    e = KEstimator("mm_rebate_v1")
    e.warm_start("0.15-0.20", 2, k0=37.4, minutes=60.0)
    # 37.4 fills/min × 60 min = 2244 fills, divided by 60 min ⇒ k=37.4
    assert e.k("0.15-0.20", 2) == 37.4


def test_window_rolls_after_seven_days():
    """A record_fill more than 7 days after window_start resets the counter."""
    e = KEstimator("mm_rebate_v1", window_days=7)
    base = datetime(2026, 4, 1, tzinfo=UTC)
    e.record_quoting_minute("0.15-0.20", 2, period_minutes=60, now=base)
    e.record_fill("0.15-0.20", 2, now=base)
    assert e.k("0.15-0.20", 2) == 1 / 60

    # Advance >7 days: counters reset
    later = base + timedelta(days=8)
    e.record_fill("0.15-0.20", 2, now=later)
    # The reset happens before recording, so we have 1 fill / 0 min
    snap = e.snapshot()[("0.15-0.20", 2)]
    assert snap["fills_count"] == 1
    assert snap["minutes_quoted"] == 0.0
    # k() must guard against division by zero
    assert e.k("0.15-0.20", 2) == 0.0


def test_independent_counters_per_bucket_and_delta():
    e = KEstimator("mm_rebate_v1")
    e.record_quoting_minute("0.15-0.20", 1, period_minutes=10)
    e.record_quoting_minute("0.15-0.20", 2, period_minutes=10)
    e.record_quoting_minute("0.20-0.30", 1, period_minutes=20)

    e.record_fill("0.15-0.20", 1)
    e.record_fill("0.20-0.30", 1)
    e.record_fill("0.20-0.30", 1)

    assert e.k("0.15-0.20", 1) == 0.1   # 1 / 10
    assert e.k("0.15-0.20", 2) == 0.0   # no fills yet
    assert e.k("0.20-0.30", 1) == 0.1   # 2 / 20


def test_snapshot_shape():
    e = KEstimator("mm_rebate_v1")
    e.warm_start("0.15-0.20", 2, k0=10.0, minutes=30.0)
    snap = e.snapshot()
    cell = snap[("0.15-0.20", 2)]
    assert set(cell.keys()) == {"fills_count", "minutes_quoted", "k_value", "window_start"}
    assert cell["k_value"] == 10.0


def test_kcounter_k_zero_division_guard():
    c = KCounter()
    assert c.k() == 0.0


def test_warm_start_does_not_pollute_other_cells():
    e = KEstimator("mm_rebate_v1")
    e.warm_start("0.15-0.20", 2, k0=37.4)
    # Other cells must remain empty
    assert e.k("0.20-0.30", 2) == 0.0
    assert e.k("0.15-0.20", 1) == 0.0
    assert e.k("0.15-0.20", 5) == 0.0
