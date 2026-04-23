from __future__ import annotations

from typing import Any

import httpx

from trading.common.config import get_settings


class InternalAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TradingAPIClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.api_token = api_token or settings.api_token
        self._http = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-TEA-Token": self.api_token}
        url = f"{self.base_url}{path}"
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.TimeoutException as e:
            raise InternalAPIError(f"timeout calling {path}") from e
        except httpx.HTTPError as e:
            raise InternalAPIError(f"network error calling {path}: {e}") from e

        try:
            payload = response.json()
        except ValueError as e:
            raise InternalAPIError(
                f"non-json response from {path} (status {response.status_code})",
                status_code=response.status_code,
            ) from e

        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
            raise InternalAPIError(
                f"{path} failed ({response.status_code}): {detail}",
                status_code=response.status_code,
            )
        if not isinstance(payload, dict):
            raise InternalAPIError(f"unexpected payload type from {path}")
        return payload

    async def get_health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def get_pnl(
        self,
        *,
        period: str = "today",
        strategy: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"period": period}
        if strategy:
            params["strategy"] = strategy
        return await self._request("GET", "/metrics/pnl", params=params)

    async def get_recent_trades(
        self,
        *,
        limit: int = 5,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if strategy:
            params["strategy"] = strategy
        return await self._request("GET", "/trades/recent", params=params)

    async def restart_service(self, service: str) -> dict[str, Any]:
        return await self._request("POST", "/system/restart", json_body={"service": service})
