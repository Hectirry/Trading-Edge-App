"""Rate limit gate (ADR 0010) — decisions isolated from DB."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from trading.llm.rate_limit import (
    DailyUsage,
    RateLimitError,
    check_before_turn,
)


@pytest.mark.asyncio
async def test_session_token_cap_raises_403() -> None:
    with pytest.raises(RateLimitError) as exc:
        await check_before_turn(
            "web:abcd",
            is_first_turn=False,
            session_tokens_so_far=200_000,
            max_sessions_per_day=50,
            max_tokens_per_session=200_000,
            max_daily_cost_usd=10.0,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_daily_cost_cap_raises_429_with_retry_after() -> None:
    fake_usage = DailyUsage(
        day=datetime.now(tz=UTC).date(),
        user_id="web:abcd",
        sessions=5,
        tokens=100,
        cost_usd=10.0,
    )
    with patch(
        "trading.llm.rate_limit.current_usage",
        new=AsyncMock(return_value=fake_usage),
    ):
        with pytest.raises(RateLimitError) as exc:
            await check_before_turn(
                "web:abcd",
                is_first_turn=False,
                session_tokens_so_far=0,
                max_sessions_per_day=50,
                max_tokens_per_session=200_000,
                max_daily_cost_usd=10.0,
            )
        assert exc.value.status_code == 429
        assert exc.value.retry_after_s is not None
        assert exc.value.retry_after_s <= 86_400


@pytest.mark.asyncio
async def test_daily_session_cap_only_on_first_turn() -> None:
    fake_usage = DailyUsage(
        day=datetime.now(tz=UTC).date(),
        user_id="web:abcd",
        sessions=50,
        tokens=100,
        cost_usd=1.0,
    )
    with patch(
        "trading.llm.rate_limit.current_usage",
        new=AsyncMock(return_value=fake_usage),
    ):
        # New session → rejected
        with pytest.raises(RateLimitError) as exc:
            await check_before_turn(
                "web:abcd",
                is_first_turn=True,
                session_tokens_so_far=0,
                max_sessions_per_day=50,
                max_tokens_per_session=200_000,
                max_daily_cost_usd=10.0,
            )
        assert exc.value.status_code == 429
        # Continuing an existing session → allowed
        await check_before_turn(
            "web:abcd",
            is_first_turn=False,
            session_tokens_so_far=0,
            max_sessions_per_day=50,
            max_tokens_per_session=200_000,
            max_daily_cost_usd=10.0,
        )


@pytest.mark.asyncio
async def test_under_caps_passes() -> None:
    fake_usage = DailyUsage(
        day=datetime.now(tz=UTC).date(),
        user_id="web:abcd",
        sessions=5,
        tokens=100,
        cost_usd=1.0,
    )
    with patch(
        "trading.llm.rate_limit.current_usage",
        new=AsyncMock(return_value=fake_usage),
    ):
        await check_before_turn(
            "web:abcd",
            is_first_turn=True,
            session_tokens_so_far=100,
            max_sessions_per_day=50,
            max_tokens_per_session=200_000,
            max_daily_cost_usd=10.0,
        )
