"""Chainlink oracle helpers + watcher pick (ADR 0012)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading.engine.features.chainlink_oracle import (
    ChainlinkSnapshot,
    DataStreamsClient,
    EACProxyClient,
    NullChainlinkClient,
    binance_chainlink_delta_bps,
    chainlink_lag_score,
    fetch_latest_cached,
    pick_watcher,
)


@dataclass
class _StubSettings:
    chainlink_datastreams_key: str = ""
    chainlink_datastreams_url: str = "https://api.dataengine.chain.link"
    chainlink_datastreams_feed_id: str = "0xabc"
    alchemy_polygon_url: str = ""
    chainlink_eac_btcusd_polygon: str = "0xc907E116054Ad103354f2D350FD2514433D57F6f"


def test_delta_bps_sign_matches_direction() -> None:
    assert binance_chainlink_delta_bps(70_100.0, 70_000.0) == pytest.approx(
        14.2857, abs=1e-3
    )
    assert binance_chainlink_delta_bps(69_900.0, 70_000.0) < 0


def test_delta_bps_zero_answer_is_zero() -> None:
    assert binance_chainlink_delta_bps(70_000.0, 0.0) == 0.0


def test_lag_score_zero_when_too_young() -> None:
    assert chainlink_lag_score(age_s=1.0, delta_bps=40.0) == 0.0


def test_lag_score_zero_when_too_small_delta() -> None:
    assert chainlink_lag_score(age_s=15.0, delta_bps=4.0) == 0.0


def test_lag_score_saturates_at_one() -> None:
    assert chainlink_lag_score(age_s=25.0, delta_bps=50.0) == pytest.approx(1.0)


def test_lag_score_monotonic_in_age() -> None:
    a = chainlink_lag_score(age_s=5.0, delta_bps=20.0)
    b = chainlink_lag_score(age_s=10.0, delta_bps=20.0)
    assert b >= a


def test_lag_score_monotonic_in_delta() -> None:
    a = chainlink_lag_score(age_s=10.0, delta_bps=10.0)
    b = chainlink_lag_score(age_s=10.0, delta_bps=25.0)
    assert b >= a


def test_pick_watcher_null_when_nothing_configured() -> None:
    assert isinstance(pick_watcher(_StubSettings()), NullChainlinkClient)


def test_pick_watcher_eac_when_only_alchemy_set() -> None:
    s = _StubSettings(alchemy_polygon_url="https://example/rpc")
    assert isinstance(pick_watcher(s), EACProxyClient)


def test_pick_watcher_datastreams_wins_when_key_set() -> None:
    s = _StubSettings(
        chainlink_datastreams_key="sk",
        alchemy_polygon_url="https://example/rpc",
    )
    assert isinstance(pick_watcher(s), DataStreamsClient)


@pytest.mark.asyncio
async def test_null_watcher_returns_none() -> None:
    w = NullChainlinkClient()
    assert await w.latest() is None


@pytest.mark.asyncio
async def test_fetch_cached_uses_cache_within_ttl() -> None:
    calls = {"n": 0}

    class _Stub:
        async def latest(self):
            calls["n"] += 1
            return ChainlinkSnapshot(
                feed="x", round_id=1, answer=70_000.0,
                updated_at_ts=0.0, source="eac_polygon",
            )

    cache: dict = {}
    w = _Stub()
    # Two back-to-back calls inside TTL → upstream called once.
    await fetch_latest_cached(w, cache, cache_ttl_s=60.0)
    await fetch_latest_cached(w, cache, cache_ttl_s=60.0)
    assert calls["n"] == 1
