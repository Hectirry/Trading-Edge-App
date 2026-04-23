from __future__ import annotations

from typing import Any

from trading.bots.telegram.api_client import TradingAPIClient


async def get_system_status(client: TradingAPIClient) -> dict[str, Any]:
    return await client.get_health()


async def get_pnl(
    client: TradingAPIClient,
    *,
    period: str = "today",
    strategy: str | None = None,
) -> dict[str, Any]:
    return await client.get_pnl(period=period, strategy=strategy)


async def get_recent_trades(
    client: TradingAPIClient,
    *,
    limit: int = 5,
    strategy: str | None = None,
) -> dict[str, Any]:
    return await client.get_recent_trades(limit=limit, strategy=strategy)


async def restart_service(client: TradingAPIClient, service: str) -> dict[str, Any]:
    return await client.restart_service(service)
