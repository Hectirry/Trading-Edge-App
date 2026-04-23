"""OpenRouter chat client (ADR 0010).

Deliberately narrow surface:
- `chat_completion()` takes model + messages and returns text + token counts.
- No tools / tool_choice / function_call are EVER sent in the request body;
  if a caller includes them in `extra` we strip them before the HTTP call.
- The response is scanned for `tool_calls` / `function_call` and raises
  `LLMPolicyError` if present — that response is discarded rather than
  persisted or shown to the user.
- Any model outside the whitelist is rejected before HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from trading.common.config import get_settings
from trading.common.logging import get_logger
from trading.llm.pricing import MODEL_PRICING, allowed, cost_usd

log = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Strip these from any caller-provided extra kwargs — we never send them.
BLOCKED_REQUEST_KEYS: tuple[str, ...] = (
    "tools",
    "tool_choice",
    "function_call",
    "functions",
)


class LLMError(RuntimeError):
    """Transport / provider error surfaced as 5xx to the caller."""


class LLMPolicyError(RuntimeError):
    """Provider returned a tool-use payload even though we never requested it.

    We discard the response rather than expose it.
    """


@dataclass
class ChatResult:
    model: str
    content: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    raw_finish_reason: str | None


async def chat_completion(
    model: str,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    referer: str | None = None,
    extra: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> ChatResult:
    if not allowed(model):
        raise LLMError(f"model not whitelisted: {model}")

    settings = get_settings()
    api_key = settings.openrouter_api_key
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY not configured")

    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "max_tokens": max_tokens or settings.llm_max_reply_tokens,
    }
    if extra:
        for k, v in extra.items():
            if k in BLOCKED_REQUEST_KEYS:
                log.warning("llm.client.blocked_key", key=k)
                continue
            body[k] = v

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": referer or "https://187-124-130-221.nip.io",
        "X-Title": "TEA Research",
    }

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=settings.llm_request_timeout_s)

    try:
        try:
            resp = await client.post(OPENROUTER_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"openrouter transport error: {e}") from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code >= 400:
        raise LLMError(f"openrouter {resp.status_code}: {resp.text[:400]}")

    try:
        data = resp.json()
    except Exception as e:
        raise LLMError(f"openrouter bad json: {e}") from e

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("openrouter returned no choices")
    choice = choices[0]
    msg = choice.get("message") or {}

    # Hard wall: if the provider returned tool-use, we discard.
    if msg.get("tool_calls") or msg.get("function_call"):
        log.error("llm.policy.tool_call_in_response", model=model)
        raise LLMPolicyError("provider returned tool-use payload; discarded")

    content = msg.get("content") or ""
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    finish_reason = choice.get("finish_reason")

    return ChatResult(
        model=model,
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd(model, tokens_in, tokens_out),
        raw_finish_reason=finish_reason,
    )


def whitelist_ids() -> list[str]:
    return sorted(MODEL_PRICING.keys())
