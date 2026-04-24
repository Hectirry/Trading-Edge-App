"""OpenRouter client — wall against tools + policy scan."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("TEA_OPENROUTER_API_KEY", "sk-or-test")

from trading.llm import client as llm_client  # noqa: E402
from trading.llm.client import (
    BLOCKED_REQUEST_KEYS,
    LLMError,
    LLMPolicyError,
    chat_completion,
)


def _ok_response(tool_call: bool = False) -> MagicMock:
    msg: dict = {"content": "hello back"}
    if tool_call:
        msg["tool_calls"] = [{"id": "x", "function": {"name": "exec", "arguments": "{}"}}]
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "choices": [{"message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return r


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from trading.common import config as cfg

    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unknown_model_rejected_before_http() -> None:
    http = AsyncMock(spec=httpx.AsyncClient)
    with pytest.raises(LLMError):
        await chat_completion(
            model="bad/model",
            messages=[{"role": "user", "content": "hi"}],
            client=http,
        )
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_request_body_never_contains_tools_keys() -> None:
    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=_ok_response())
    result = await chat_completion(
        model="qwen/qwen3-max",
        messages=[{"role": "user", "content": "hi"}],
        extra={"tools": [{"type": "function"}], "tool_choice": "auto", "temperature": 0.1},
        client=http,
    )
    assert result.content == "hello back"
    body = http.post.await_args.kwargs["json"]
    for k in BLOCKED_REQUEST_KEYS:
        assert k not in body, f"{k} leaked into request body"
    assert body.get("temperature") == 0.1  # non-blocked extra passes through


@pytest.mark.asyncio
async def test_tool_call_in_response_raises_policy_error() -> None:
    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=_ok_response(tool_call=True))
    with pytest.raises(LLMPolicyError):
        await chat_completion(
            model="qwen/qwen3-max",
            messages=[{"role": "user", "content": "hi"}],
            client=http,
        )


@pytest.mark.asyncio
async def test_5xx_becomes_llm_error() -> None:
    http = AsyncMock(spec=httpx.AsyncClient)
    bad = MagicMock()
    bad.status_code = 502
    bad.text = "upstream down"
    http.post = AsyncMock(return_value=bad)
    with pytest.raises(LLMError):
        await chat_completion(
            model="qwen/qwen3-max",
            messages=[{"role": "user", "content": "hi"}],
            client=http,
        )


@pytest.mark.asyncio
async def test_usage_and_cost_surfaced() -> None:
    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=_ok_response())
    r = await chat_completion(
        model="qwen/qwen3-max",
        messages=[{"role": "user", "content": "hi"}],
        client=http,
    )
    assert r.tokens_in == 10
    assert r.tokens_out == 20
    # qwen3-max: 0.78/M in + 3.90/M out → 10*0.78/M + 20*3.90/M = 0.000086
    assert r.cost_usd == pytest.approx(0.000086, abs=1e-6)


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    with patch.object(llm_client, "get_settings") as gs:
        gs.return_value = type(
            "S",
            (),
            {"openrouter_api_key": "", "llm_max_reply_tokens": 512, "llm_request_timeout_s": 30.0},
        )()
        with pytest.raises(LLMError):
            await chat_completion(
                model="qwen/qwen3-max",
                messages=[{"role": "user", "content": "hi"}],
            )
