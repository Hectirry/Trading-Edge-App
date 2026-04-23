"""Per-user daily counters + per-session token cap (ADR 0010)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from trading.common.db import acquire


class RateLimitError(Exception):
    def __init__(self, reason: str, status_code: int, retry_after_s: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.retry_after_s = retry_after_s


@dataclass
class DailyUsage:
    day: date
    user_id: str
    sessions: int
    tokens: int
    cost_usd: float


async def current_usage(user_id: str) -> DailyUsage:
    today = datetime.now(tz=UTC).date()
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT day, user_id, sessions, tokens, cost_usd "
            "FROM research.llm_usage_daily WHERE day = $1 AND user_id = $2",
            today,
            user_id,
        )
    if row is None:
        return DailyUsage(day=today, user_id=user_id, sessions=0, tokens=0, cost_usd=0.0)
    return DailyUsage(
        day=row["day"],
        user_id=row["user_id"],
        sessions=int(row["sessions"]),
        tokens=int(row["tokens"]),
        cost_usd=float(row["cost_usd"]),
    )


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(tz=UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((tomorrow - now).total_seconds()))


async def check_before_turn(
    user_id: str,
    *,
    is_first_turn: bool,
    session_tokens_so_far: int,
    max_sessions_per_day: int,
    max_tokens_per_session: int,
    max_daily_cost_usd: float,
) -> None:
    if session_tokens_so_far >= max_tokens_per_session:
        raise RateLimitError(
            f"session capped at {max_tokens_per_session} tokens; /ask_reset",
            status_code=403,
        )
    usage = await current_usage(user_id)
    if float(usage.cost_usd) >= float(max_daily_cost_usd):
        raise RateLimitError(
            f"daily cost cap ${max_daily_cost_usd:.2f} reached",
            status_code=429,
            retry_after_s=_seconds_until_utc_midnight(),
        )
    if is_first_turn and usage.sessions >= max_sessions_per_day:
        raise RateLimitError(
            f"daily session cap {max_sessions_per_day} reached",
            status_code=429,
            retry_after_s=_seconds_until_utc_midnight(),
        )


async def record_turn(
    user_id: str,
    *,
    is_first_turn: bool,
    tokens_added: int,
    cost_added_usd: float,
) -> None:
    today = datetime.now(tz=UTC).date()
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research.llm_usage_daily (day, user_id, sessions, tokens, cost_usd)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (day, user_id) DO UPDATE
              SET sessions = research.llm_usage_daily.sessions + EXCLUDED.sessions,
                  tokens   = research.llm_usage_daily.tokens   + EXCLUDED.tokens,
                  cost_usd = research.llm_usage_daily.cost_usd + EXCLUDED.cost_usd
            """,
            today,
            user_id,
            1 if is_first_turn else 0,
            tokens_added,
            cost_added_usd,
        )
