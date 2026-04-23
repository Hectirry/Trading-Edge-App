from __future__ import annotations

from dataclasses import dataclass

import httpx

from trading.common.config import get_settings


class ServiceControlError(RuntimeError):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ResolvedService:
    requested_name: str
    container_name: str


def _service_aliases() -> dict[str, str]:
    raw = get_settings().restart_service_map
    aliases: dict[str, str] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        alias, container = item.split("=", 1)
        alias = alias.strip().lower()
        container = container.strip()
        if alias and container:
            aliases[alias] = container
    return aliases


def resolve_service(name: str) -> ResolvedService:
    requested = name.strip().lower()
    aliases = _service_aliases()
    container = aliases.get(requested)
    if not container:
        supported = ", ".join(sorted(aliases)) or "(none configured)"
        raise ServiceControlError(
            f"service '{name}' is not restartable; allowed values: {supported}",
            status_code=404,
        )
    return ResolvedService(requested_name=requested, container_name=container)


async def restart_service(name: str) -> ResolvedService:
    settings = get_settings()
    if not settings.docker_restart_enabled:
        raise ServiceControlError(
            "service restart is disabled; set TEA_DOCKER_RESTART_ENABLED=true to enable it",
            status_code=503,
        )

    resolved = resolve_service(name)
    transport = httpx.AsyncHTTPTransport(uds=settings.docker_socket_path)
    async with httpx.AsyncClient(
        base_url="http://docker",
        transport=transport,
        timeout=15.0,
    ) as client:
        inspect_resp = await client.get(f"/containers/{resolved.container_name}/json")
        if inspect_resp.status_code == 404:
            raise ServiceControlError(
                f"container '{resolved.container_name}' not found on Docker host",
                status_code=404,
            )
        if inspect_resp.status_code >= 400:
            raise ServiceControlError(
                f"docker inspect failed with status {inspect_resp.status_code}",
                status_code=502,
            )

        restart_resp = await client.post(
            f"/containers/{resolved.container_name}/restart",
            params={"t": 10},
        )
        if restart_resp.status_code == 204:
            return resolved
        if restart_resp.status_code == 404:
            raise ServiceControlError(
                f"container '{resolved.container_name}' disappeared before restart",
                status_code=404,
            )
        raise ServiceControlError(
            f"docker restart failed with status {restart_resp.status_code}",
            status_code=502,
        )
