"""/ask default context refs + wildcard recent_trades (post-LLM-bug fix).

When /ask is called without explicit context, the bot must inject:
  - recent_trades:*:10
  - paper_stats:<strategy>:7  for each active strategy returned by
    /api/v1/strategies

Otherwise the model replies "no tengo acceso" because the <context>
block is empty.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("TEA_API_TOKEN", "testtoken")
os.environ.setdefault("TEA_TELEGRAM_BOT_TOKEN", "bot-token")
os.environ.setdefault("TEA_TELEGRAM_AUTHORIZED_USERS", "111,222")

from trading.bots.telegram.commands import CommandPoller


def _update(user_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


@pytest.fixture
def poller():
    from trading.common import config as cfg

    cfg.get_settings.cache_clear()
    p = CommandPoller()
    p._reply = AsyncMock()
    p._api_get = AsyncMock(return_value=(200, {}))
    p._api_post = AsyncMock(return_value=(200, {"assistant": "ok", "model": "qwen/qwen3-max"}))
    # Don't hit real Redis during tests.
    p._get_or_create_session = AsyncMock(return_value=("tg-42-abc", True))
    return p


@pytest.mark.asyncio
async def test_ask_injects_defaults_when_no_explicit_refs(poller) -> None:
    poller._api_get.return_value = (
        200,
        {"strategies": [{"name": "imbalance_v3"}, {"name": "trend_confirm_t1_v1"}]},
    )
    await poller._handle_update(_update(111, 42, "/ask cómo vamos?"))
    poller._api_post.assert_awaited()
    # Find the /api/v1/llm/chat call (first post is that; any subsequent
    # reply-send posts go through _reply which is mocked separately).
    call = next(
        c for c in poller._api_post.await_args_list if c.args and c.args[0] == "/api/v1/llm/chat"
    )
    body = call.kwargs["json_body"]
    refs = body["context_refs"]
    types_ids = {(r["type"], r["id"]) for r in refs}
    assert ("recent_trades", "*:10") in types_ids
    assert ("paper_stats", "imbalance_v3:7") in types_ids
    assert ("paper_stats", "trend_confirm_t1_v1:7") in types_ids


@pytest.mark.asyncio
async def test_ask_usage_shown_when_no_args(poller) -> None:
    await poller._handle_update(_update(111, 42, "/ask"))
    poller._api_post.assert_not_awaited()
    poller._reply.assert_awaited()
    msg = poller._reply.await_args.args[1]
    assert "usage" in msg
    assert "defaults" in msg


@pytest.mark.asyncio
async def test_ask_defaults_survive_strategies_endpoint_failure(poller) -> None:
    # Simulate /api/v1/strategies returning a non-200 — we should still send
    # at least recent_trades:*:10 so the model has something to ground on.
    poller._api_get.return_value = (503, "service down")
    await poller._handle_update(_update(111, 42, "/ask qué pasó?"))
    call = next(
        c for c in poller._api_post.await_args_list if c.args and c.args[0] == "/api/v1/llm/chat"
    )
    refs = call.kwargs["json_body"]["context_refs"]
    assert {"type": "recent_trades", "id": "*:10"} in refs
    # no paper_stats refs because we couldn't list strategies
    assert not any(r["type"] == "paper_stats" for r in refs)
