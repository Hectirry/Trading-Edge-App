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
