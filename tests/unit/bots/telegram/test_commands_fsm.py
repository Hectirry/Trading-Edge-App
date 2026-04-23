"""Tests for the Telegram command poller — authorization + /killswitch FSM."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

# Ensure settings don't hit a real secrets file during import.
os.environ.setdefault("TEA_API_TOKEN", "testtoken")
os.environ.setdefault("TEA_TELEGRAM_BOT_TOKEN", "bot-token")
os.environ.setdefault("TEA_TELEGRAM_AUTHORIZED_USERS", "111,222")

from trading.bots.telegram.commands import KILLSWITCH_CONFIRM, CommandPoller


def _update(user_id: int, chat_id: int, text: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


@pytest.fixture
def poller():
    # Re-read env under the patched values.
    from trading.common import config as cfg

    cfg.get_settings.cache_clear()
    p = CommandPoller()
    # Replace outbound dependencies with AsyncMocks.
    p._reply = AsyncMock()
    p._api_get = AsyncMock(return_value=(200, {}))
    p._api_post = AsyncMock(return_value=(200, {}))
    return p


@pytest.mark.asyncio
async def test_unauthorized_user_rejected(poller) -> None:
    await poller._handle_update(_update(999, 1, "/status"))
    poller._reply.assert_awaited_once()
    # did NOT hit the API
    poller._api_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_killswitch_requires_exact_confirmation(poller) -> None:
    # Step 1: /killswitch prompts for confirm phrase.
    await poller._handle_update(_update(111, 42, "/killswitch"))
    assert 111 in poller._killswitch_pending
    # Step 2: wrong phrase cancels — no API call, FSM cleared.
    await poller._handle_update(_update(111, 42, "nope"))
    assert 111 not in poller._killswitch_pending
    poller._api_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_killswitch_correct_phrase_arms(poller) -> None:
    poller._api_post.return_value = (200, {"path": "/var/tea/control/KILL_SWITCH"})
    await poller._handle_update(_update(111, 42, "/killswitch"))
    await poller._handle_update(_update(111, 42, KILLSWITCH_CONFIRM))
    poller._api_post.assert_awaited_once()
    called_path = poller._api_post.await_args.args[0]
    assert called_path == "/api/v1/killswitch"
    assert 111 not in poller._killswitch_pending


@pytest.mark.asyncio
async def test_pause_requires_strategy_arg(poller) -> None:
    await poller._handle_update(_update(111, 42, "/pause"))
    poller._api_post.assert_not_awaited()
    poller._reply.assert_awaited()


@pytest.mark.asyncio
async def test_pause_with_name_calls_api(poller) -> None:
    await poller._handle_update(_update(111, 42, "/pause imbalance_v3"))
    poller._api_post.assert_awaited_once_with("/api/v1/strategies/imbalance_v3/pause")


@pytest.mark.asyncio
async def test_backtest_needs_four_args(poller) -> None:
    await poller._handle_update(_update(111, 42, "/backtest only two args"))
    poller._api_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_backtest_happy_path_queues_job(poller) -> None:
    poller._api_post.return_value = (200, {"job_id": "job-abc"})

    # Prevent the follow-up background task from polling for 20 min, while
    # still closing the coroutine so we don't emit a "never awaited" warning.
    def _consume(coro):
        coro.close()
        return None

    with patch(
        "trading.bots.telegram.commands.asyncio.create_task", side_effect=_consume
    ) as spawn:
        await poller._handle_update(
            _update(
                111,
                42,
                "/backtest imbalance_v3 config/strategies/x.toml "
                "2026-04-10T00:00:00Z 2026-04-22T00:00:00Z",
            )
        )
        spawn.assert_called_once()
    poller._api_post.assert_awaited_once()
    path, kwargs = poller._api_post.await_args.args[0], poller._api_post.await_args.kwargs
    assert path == "/api/v1/backtests"
    body = kwargs["json_body"]
    assert body["strategy"] == "imbalance_v3"
    assert body["requested_by"] == "telegram:111"


@pytest.mark.asyncio
async def test_unknown_command_reports_help(poller) -> None:
    await poller._handle_update(_update(111, 42, "/nonsense"))
    poller._api_get.assert_not_awaited()
    poller._api_post.assert_not_awaited()
    poller._reply.assert_awaited_once()


# ---- /status: tolerate real /api/v1/status shape (pnl_today is a dict) ----

# Real response from api.main.api_status():
#   {
#     "engine_up": bool,
#     "heartbeat_age_s": float|None,
#     "strategies": [{"name": str, "paused": bool, ...}],
#     "pnl_today": {"pnl": float, "n_trades": int},   # <-- NOT a float
#     "kill_switch_active": bool,
#   }
# plus a second call to /api/v1/positions → {"positions": [...]}

_STATUS_PAYLOAD = {
    "engine_up": True,
    "heartbeat_age_s": 12.3,
    "strategies": [
        {"name": "imbalance_v3", "paused": False},
        {"name": "trend_confirm_t1_v1", "paused": True},
    ],
    "pnl_today": {"pnl": -1.25, "n_trades": 3},
    "kill_switch_active": False,
}
_POSITIONS_PAYLOAD = {"positions": [{"strategy_id": "imbalance_v3"}]}


@pytest.mark.asyncio
async def test_status_parses_nested_pnl_dict(poller) -> None:
    """Regression: /status used to TypeError on _fmt_money(dict)."""

    async def fake_get(path, params=None):  # noqa: ARG001
        if path == "/api/v1/status":
            return 200, _STATUS_PAYLOAD
        if path == "/api/v1/positions":
            return 200, _POSITIONS_PAYLOAD
        return 404, {}

    poller._api_get = AsyncMock(side_effect=fake_get)
    await poller._handle_update(_update(111, 42, "/status"))
    poller._reply.assert_awaited_once()
    msg = poller._reply.await_args.args[1]
    # Must have rendered the nested pnl, trades, and kill switch state.
    assert "pnl today: $-1.25" in msg
    assert "trades today: 3" in msg
    assert "kill switch: OFF" in msg
    assert "engine: up" in msg
    assert "heartbeat age=12.3s" in msg
    assert "strategies: 2 (1 paused)" in msg
    assert "open positions: 1" in msg


@pytest.mark.asyncio
async def test_status_handles_missing_pnl_today(poller) -> None:
    payload = {
        "engine_up": False,
        "heartbeat_age_s": None,
        "strategies": [],
        "pnl_today": None,
        "kill_switch_active": True,
    }

    async def fake_get(path, params=None):  # noqa: ARG001
        if path == "/api/v1/status":
            return 200, payload
        if path == "/api/v1/positions":
            return 200, {"positions": []}
        return 404, {}

    poller._api_get = AsyncMock(side_effect=fake_get)
    await poller._handle_update(_update(111, 42, "/status"))
    msg = poller._reply.await_args.args[1]
    assert "engine: DOWN" in msg
    assert "kill switch: ON" in msg
    assert "pnl today: -" in msg
    assert "trades today: -" in msg


# ---- /pnl: uses period= query param (today|semana|mes), not hours= ----


@pytest.mark.asyncio
async def test_pnl_defaults_to_today_period(poller) -> None:
    poller._api_get.return_value = (200, {"pnl": 5.5, "n_trades": 2})
    await poller._handle_update(_update(111, 42, "/pnl"))
    poller._api_get.assert_awaited_once_with(
        "/api/v1/pnl", params={"period": "today"}
    )
    msg = poller._reply.await_args.args[1]
    assert "pnl (today)" in msg
    assert "$5.50" in msg


@pytest.mark.asyncio
async def test_pnl_accepts_semana_and_mes(poller) -> None:
    poller._api_get.return_value = (200, {"pnl": 0.0, "n_trades": 0})
    await poller._handle_update(_update(111, 42, "/pnl semana"))
    await poller._handle_update(_update(111, 42, "/pnl mes"))
    periods = [
        call.kwargs["params"]["period"] for call in poller._api_get.await_args_list
    ]
    assert periods == ["semana", "mes"]


@pytest.mark.asyncio
async def test_pnl_rejects_unknown_period(poller) -> None:
    await poller._handle_update(_update(111, 42, "/pnl 24"))
    poller._api_get.assert_not_awaited()
    msg = poller._reply.await_args.args[1]
    assert "today|semana|mes" in msg


@pytest.mark.asyncio
async def test_restart_requires_service_arg(poller) -> None:
    await poller._handle_update(_update(111, 42, "/restart"))
    msg = poller._reply.await_args.args[1]
    assert "usage: /restart <service>" in msg


@pytest.mark.asyncio
async def test_restart_calls_control_api(poller) -> None:
    poller._control_api.restart_service = AsyncMock(
        return_value={"requested_service": "engine", "container_name": "tea-engine"}
    )
    await poller._handle_update(_update(111, 42, "/restart engine"))
    poller._control_api.restart_service.assert_awaited_once_with("engine")
    msg = poller._reply.await_args.args[1]
    assert "restarted engine" in msg
