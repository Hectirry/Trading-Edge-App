"""End-to-end orchestration for one /ask turn (ADR 0010).

Callers provide ``session_id``, ``user_id``, ``message``, optional
``context_refs``, and a model. This module:

1. validates the model whitelist,
2. loads prior turns from research.llm_conversations (if any),
3. enforces daily + per-session rate limits,
4. loads context refs (read-only DB),
5. builds the message list with the hard-wall system prompt,
6. calls OpenRouter,
7. persists the updated conversation + bumps daily usage,
8. returns the assistant reply + running totals.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.common.config import get_settings
from trading.common.logging import get_logger
from trading.llm.client import ChatResult, LLMError, chat_completion
from trading.llm.context_loader import ContextRef, load_contexts
from trading.llm.pricing import allowed
from trading.llm.prompt import build_messages
from trading.llm.rate_limit import check_before_turn, record_turn
from trading.llm.store import Conversation, get_by_session, upsert_turn

log = get_logger(__name__)


@dataclass
class TurnResult:
    assistant_content: str
    conversation: Conversation
    chat_result: ChatResult


async def run_turn(
    *,
    session_id: str,
    user_id: str,
    message: str,
    context_refs: list[dict] | None = None,
    model: str | None = None,
) -> TurnResult:
    settings = get_settings()
    model = model or settings.llm_default_model
    if not allowed(model):
        raise LLMError(f"model not whitelisted: {model}")

    refs = [ContextRef.parse(r) for r in (context_refs or [])]

    prior = await get_by_session(session_id)
    prior_messages = prior.messages if prior else []
    session_tokens_so_far = (prior.tokens_in + prior.tokens_out) if prior else 0
    is_first_turn = prior is None

    await check_before_turn(
        user_id,
        is_first_turn=is_first_turn,
        session_tokens_so_far=session_tokens_so_far,
        max_sessions_per_day=settings.llm_max_sessions_per_day,
        max_tokens_per_session=settings.llm_max_tokens_per_session,
        max_daily_cost_usd=settings.llm_max_daily_cost_usd,
    )

    loaded = await load_contexts(refs)
    outgoing = build_messages(
        user_message=message,
        history=[m for m in prior_messages if m.get("role") != "system"],
        contexts=loaded,
    )

    chat_result = await chat_completion(
        model=model,
        messages=outgoing,
        max_tokens=settings.llm_max_reply_tokens,
    )

    # Update thread: drop the transient system, keep history as user/assistant
    # turns so the next turn's builder can re-compose a fresh system with the
    # current context. Store only stable turns.
    new_history: list[dict] = [m for m in prior_messages if m.get("role") != "system"]
    new_history.append({"role": "user", "content": message})
    new_history.append({"role": "assistant", "content": chat_result.content})

    tokens_in_total = (prior.tokens_in if prior else 0) + chat_result.tokens_in
    tokens_out_total = (prior.tokens_out if prior else 0) + chat_result.tokens_out
    cost_total = (prior.cost_usd if prior else 0.0) + chat_result.cost_usd

    conversation = await upsert_turn(
        session_id=session_id,
        user_id=user_id,
        model=model,
        full_messages=new_history,
        context_refs=[{"type": r.type, "id": r.id} for r in refs],
        tokens_in_total=tokens_in_total,
        tokens_out_total=tokens_out_total,
        cost_usd_total=cost_total,
    )

    await record_turn(
        user_id,
        is_first_turn=is_first_turn,
        tokens_added=chat_result.tokens_in + chat_result.tokens_out,
        cost_added_usd=chat_result.cost_usd,
    )

    log.info(
        "llm.turn.ok",
        session_id=session_id,
        user_id=user_id,
        model=model,
        tokens_in=chat_result.tokens_in,
        tokens_out=chat_result.tokens_out,
        cost_usd=chat_result.cost_usd,
        n_refs=len(refs),
    )
    return TurnResult(
        assistant_content=chat_result.content,
        conversation=conversation,
        chat_result=chat_result,
    )
