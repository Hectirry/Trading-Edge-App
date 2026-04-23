from __future__ import annotations

import os

import pytest

os.environ.setdefault("TEA_RESTART_SERVICE_MAP", "engine=tea-engine,api=tea-api")

from trading.api.service_control import ServiceControlError, resolve_service
from trading.common import config as cfg


def test_resolve_service_maps_alias_to_container() -> None:
    cfg.get_settings.cache_clear()
    resolved = resolve_service("engine")
    assert resolved.requested_name == "engine"
    assert resolved.container_name == "tea-engine"


def test_resolve_service_rejects_unknown_service() -> None:
    cfg.get_settings.cache_clear()
    with pytest.raises(ServiceControlError) as exc:
        resolve_service("unknown")
    assert "not restartable" in str(exc.value)
