"""Chainlink oracle signal (ADR 0012).

Abstracts two backends behind a single ``ChainlinkWatcher`` protocol:

- :class:`DataStreamsClient` — primary. Pull REST from the Chainlink
  Data Streams endpoint (the same feed Polymarket resolves against).
  Activates when ``TEA_CHAINLINK_DATASTREAMS_KEY`` is set.
- :class:`EACProxyClient` — fallback. Read ``latestRoundData()`` from
  the Polygon EACAggregatorProxy via an Alchemy RPC URL. Proxy feed,
  not the resolution source — interpret as "oracle catching up to
  Binance spot".
- :class:`NullChainlinkClient` — keys missing. Always returns ``None``
  so strategies SKIP cleanly.

``pick_watcher()`` inspects settings and returns the best available.

Signal helpers:

- ``chainlink_lag_score(age_s, delta_bps)`` — monotonically
  non-decreasing in both age and |delta|; saturates at 1.0.
- ``binance_chainlink_delta_bps(spot_binance, answer)`` — sign encodes
  direction where Binance is leading.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChainlinkSnapshot:
    feed: str
    round_id: int
    answer: float
    updated_at_ts: float
    source: str  # 'datastreams' | 'eac_polygon'


class ChainlinkWatcher(Protocol):
    async def latest(self) -> ChainlinkSnapshot | None: ...


class NullChainlinkClient:
    async def latest(self) -> ChainlinkSnapshot | None:
        return None


class DataStreamsClient:
    """Pull-based client for the Polymarket-grade Chainlink feed.

    The protocol + exact envelope for Polymarket's Data Streams feed
    is under controlled preview and the operator-facing key is pending
    on ``pm-ds-request.streams.chain.link``. We encode the canonical
    REST shape here; once the key is in ``TEA_CHAINLINK_DATASTREAMS_KEY``
    the ingestor wakes up and starts writing `source='datastreams'`
    rows into ``market_data.chainlink_updates``.

    If the upstream response shape differs from what we expect the
    client logs a warning and returns ``None`` (strategies downgrade
    to the EAC fallback or abstain).
    """

    def __init__(self, *, api_key: str, base_url: str, feed_id: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.feed_id = feed_id

    async def latest(self) -> ChainlinkSnapshot | None:
        import httpx

        url = f"{self.base_url}/v1/reports/latest?feedID={self.feed_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url, headers=headers)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
            price = float(data["price"]) / 1e8
            observations_ts = float(data["observationsTimestamp"])
            round_id = int(data.get("reportContext", [0, 0])[1])
        except Exception:
            return None
        return ChainlinkSnapshot(
            feed=f"datastreams:{self.feed_id}",
            round_id=round_id,
            answer=price,
            updated_at_ts=observations_ts,
            source="datastreams",
        )


class EACProxyClient:
    """Polygon EACAggregatorProxy client (BTC/USD 8-decimal).

    Reads ``latestRoundData()`` over JSON-RPC. Minimal implementation —
    hand-rolled `eth_call` so we don't pull ``web3`` into the hot
    strategy path. Falls back to ``None`` on any parse error.
    """

    _LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"

    def __init__(self, *, rpc_url: str, contract_address: str) -> None:
        self.rpc_url = rpc_url
        self.contract_address = contract_address.lower()

    async def latest(self) -> ChainlinkSnapshot | None:
        import httpx

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": self.contract_address, "data": self._LATEST_ROUND_DATA_SELECTOR},
                "latest",
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(self.rpc_url, json=body)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            raw = r.json()["result"]
            if not raw or raw == "0x":
                return None
            data = bytes.fromhex(raw[2:])
            # Layout: roundId(uint80)|answer(int256)|startedAt(uint256)|
            #         updatedAt(uint256)|answeredInRound(uint80)
            # padded to 32 bytes each → 5 * 32 = 160 bytes.
            if len(data) < 160:
                return None
            round_id = int.from_bytes(data[0:32], "big")
            answer_raw = int.from_bytes(data[32:64], "big", signed=True)
            updated_at = int.from_bytes(data[96:128], "big")
        except Exception:
            return None
        return ChainlinkSnapshot(
            feed=f"eac_polygon:{self.contract_address}",
            round_id=round_id,
            answer=float(answer_raw) / 1e8,
            updated_at_ts=float(updated_at),
            source="eac_polygon",
        )


def pick_watcher(settings) -> ChainlinkWatcher:
    if settings.chainlink_datastreams_key:
        return DataStreamsClient(
            api_key=settings.chainlink_datastreams_key,
            base_url=settings.chainlink_datastreams_url,
            feed_id=settings.chainlink_datastreams_feed_id,
        )
    if settings.alchemy_polygon_url:
        return EACProxyClient(
            rpc_url=settings.alchemy_polygon_url,
            contract_address=settings.chainlink_eac_btcusd_polygon,
        )
    return NullChainlinkClient()


# --------------------------------------------------------------- signals


def binance_chainlink_delta_bps(spot_binance: float, chainlink_answer: float) -> float:
    if chainlink_answer <= 0:
        return 0.0
    return (spot_binance / chainlink_answer - 1.0) * 10_000.0


def chainlink_lag_score(age_s: float, delta_bps: float) -> float:
    """Bounded [0, 1]. Zero when age_s < 2 OR |delta_bps| < 5. Saturates
    at age ≥ 20s AND |delta| ≥ 40 bps.
    """
    if age_s < 2 or abs(delta_bps) < 5:
        return 0.0
    age_score = min(1.0, max(0.0, (age_s - 2) / 18.0))
    delta_score = min(1.0, max(0.0, (abs(delta_bps) - 5) / 35.0))
    return age_score * delta_score


async def fetch_latest_cached(
    watcher: ChainlinkWatcher,
    cache: dict,
    cache_ttl_s: float = 10.0,
) -> ChainlinkSnapshot | None:
    """Cache wrapper so multiple strategies sharing one watcher don't
    hammer the upstream. ``cache`` is a plain dict injected by the
    caller (e.g. one per strategy or one global singleton).
    """
    now = asyncio.get_event_loop().time()
    hit = cache.get("snapshot")
    ts = cache.get("ts", 0.0)
    if hit is not None and (now - ts) < cache_ttl_s:
        return hit  # type: ignore[return-value]
    snap = await watcher.latest()
    cache["snapshot"] = snap
    cache["ts"] = now
    return snap
