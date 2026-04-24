"""Wildcard strategy_id in recent_trades context (ADR 0010 default refs)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from trading.llm.context_loader import ContextRef, _load_recent_trades


class _FakeConn:
    def __init__(self, *, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return list(self.rows)


@asynccontextmanager
async def _fake_acquire(conn):
    yield conn


@pytest.mark.asyncio
async def test_wildcard_strategy_drops_filter() -> None:
    conn = _FakeConn(rows=[])
    with patch(
        "trading.llm.context_loader.acquire",
        new=lambda: _fake_acquire(conn),
    ):
        ref = ContextRef(type="recent_trades", id="*:10")
        loaded = await _load_recent_trades(ref)
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    # Wildcard branch binds only N (no strategy_id parameter).
    assert args == (10,)
    assert "strategy_id = $1" not in sql
    assert "o.strategy_id" in sql  # it's still selected for output
    assert "[]" in loaded.body  # rows=[] → empty JSON list


@pytest.mark.asyncio
async def test_explicit_strategy_applies_filter() -> None:
    conn = _FakeConn(rows=[])
    with patch(
        "trading.llm.context_loader.acquire",
        new=lambda: _fake_acquire(conn),
    ):
        ref = ContextRef(type="recent_trades", id="trend_confirm_t1_v1:20")
        await _load_recent_trades(ref)
    sql, args = conn.calls[0]
    assert args == ("trend_confirm_t1_v1", 20)
    assert "WHERE o.mode = 'paper' AND o.strategy_id = $1" in sql


@pytest.mark.asyncio
async def test_wildcard_rows_include_strategy_id_column() -> None:
    """We need strategy_id in the output so the model can tell which
    strategy each trade belongs to when the ref is wildcarded."""
    rows = [
        {
            "ts_submit": "2026-04-23T00:00:00Z",
            "strategy_id": "trend_confirm_t1_v1",
            "instrument_id": "btc-updown-5m-x",
            "entry_price": "0.51",
            "pnl": "0.09",
            "resolution": "YES",
        }
    ]
    conn = _FakeConn(rows=rows)
    with patch(
        "trading.llm.context_loader.acquire",
        new=lambda: _fake_acquire(conn),
    ):
        loaded = await _load_recent_trades(ContextRef(type="recent_trades", id="*:5"))
    assert '"strategy_id": "trend_confirm_t1_v1"' in loaded.body
