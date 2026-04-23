from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("TEA_API_TOKEN", "testtoken")

from trading.bots.telegram.api_client import InternalAPIError, TradingAPIClient


@pytest.mark.asyncio
async def test_get_health_uses_expected_path() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        assert request.headers["X-TEA-Token"] == "testtoken"
        return httpx.Response(200, json={"ok": True, "engine_up": True})

    transport = httpx.MockTransport(handler)
    client = TradingAPIClient(base_url="http://tea-api", api_token="testtoken")
    await client.aclose()
    client._http = httpx.AsyncClient(transport=transport)
    try:
        payload = await client.get_health()
    finally:
        await client.aclose()
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_restart_service_surfaces_api_error_detail() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "service restart is disabled"})

    transport = httpx.MockTransport(handler)
    client = TradingAPIClient(base_url="http://tea-api", api_token="testtoken")
    await client.aclose()
    client._http = httpx.AsyncClient(transport=transport)
    with pytest.raises(InternalAPIError) as exc:
        await client.restart_service("engine")
    await client.aclose()
    assert "service restart is disabled" in str(exc.value)
