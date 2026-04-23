"""Persistence helpers for research.llm_conversations (ADR 0010).

Row is upserted on every turn: the ``messages`` JSONB holds the whole
thread for that ``session_id``, and ``tokens_*`` / ``cost_usd`` are
running totals.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from trading.common.db import acquire


@dataclass
class Conversation:
    id: uuid.UUID
    session_id: str
    user_id: str
    model: str
    messages: list[dict]
    context_refs: list[dict]
    tokens_in: int
    tokens_out: int
    cost_usd: float


async def get_by_session(session_id: str) -> Conversation | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, session_id, user_id, model, messages, context_refs, "
            "tokens_in, tokens_out, cost_usd "
            "FROM research.llm_conversations WHERE session_id = $1",
            session_id,
        )
    if row is None:
        return None
    return _row_to_conversation(row)


async def upsert_turn(
    *,
    session_id: str,
    user_id: str,
    model: str,
    full_messages: list[dict],
    context_refs: list[dict],
    tokens_in_total: int,
    tokens_out_total: int,
    cost_usd_total: float,
) -> Conversation:
    """Create the row on first turn; overwrite running totals on subsequent.

    We persist the whole ``messages`` JSONB each time because conversations
    are short (tens of turns at most) and this simplifies replay + the UI.
    """
    new_id = uuid.uuid4()
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO research.llm_conversations
                (id, session_id, user_id, model, messages, context_refs,
                 tokens_in, tokens_out, cost_usd, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb,
                    $7, $8, $9, now(), now())
            ON CONFLICT (session_id) DO UPDATE
              SET messages     = EXCLUDED.messages,
                  context_refs = EXCLUDED.context_refs,
                  tokens_in    = EXCLUDED.tokens_in,
                  tokens_out   = EXCLUDED.tokens_out,
                  cost_usd     = EXCLUDED.cost_usd,
                  model        = EXCLUDED.model,
                  updated_at   = now()
            RETURNING id, session_id, user_id, model, messages, context_refs,
                      tokens_in, tokens_out, cost_usd
            """,
            new_id,
            session_id,
            user_id,
            model,
            json.dumps(full_messages),
            json.dumps(context_refs),
            tokens_in_total,
            tokens_out_total,
            cost_usd_total,
        )
    return _row_to_conversation(row)


def _row_to_conversation(row: Any) -> Conversation:
    msgs = row["messages"]
    refs = row["context_refs"]
    if isinstance(msgs, str):
        msgs = json.loads(msgs)
    if isinstance(refs, str):
        refs = json.loads(refs)
    return Conversation(
        id=row["id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        model=row["model"],
        messages=list(msgs or []),
        context_refs=list(refs or []),
        tokens_in=int(row["tokens_in"]),
        tokens_out=int(row["tokens_out"]),
        cost_usd=float(row["cost_usd"]),
    )
