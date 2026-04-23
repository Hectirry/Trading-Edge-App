from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class BacktestSummary(BaseModel):
    id: str
    strategy_name: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    dataset_from: datetime
    dataset_to: datetime
    n_trades: int | None = None
    total_pnl: float | None = None
    win_rate: float | None = None
    sharpe_per_trade: float | None = None
    mdd_usd: float | None = None


class NewBacktestRequest(BaseModel):
    strategy: str
    params_file: str
    from_ts: datetime
    to_ts: datetime
    source: Literal["polybot_sqlite", "paper_ticks"] = "polybot_sqlite"
    polybot_db: str = "/polybot-btc5m-data/polybot.db"
    slug_encodes_open_ts: bool = False
    requested_by: str = "web:unknown"


class JobStatus(BaseModel):
    id: str
    status: str
    strategy_name: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    backtest_id: str | None
    stdout_tail: str | None
    stderr_tail: str | None
    error_message: str | None
    requested_by: str


class PauseResponse(BaseModel):
    strategy: str
    paused: bool
    by: str


class KillswitchRequest(BaseModel):
    confirm: str


class KillswitchResponse(BaseModel):
    active: bool
    path: str
    at: datetime


class RestartServiceRequest(BaseModel):
    service: str


class RestartServiceResponse(BaseModel):
    requested_service: str
    container_name: str
    status: str
    detail: str
    restarted_at: datetime


class LLMContextRef(BaseModel):
    type: Literal["backtest", "strategy", "recent_trades", "paper_stats", "adr"]
    id: str


class LLMChatRequest(BaseModel):
    session_id: str
    message: str
    context_refs: list[LLMContextRef] = []
    model: str | None = None


class LLMChatResponse(BaseModel):
    session_id: str
    assistant: str
    model: str
    tokens_in_total: int
    tokens_out_total: int
    cost_usd_total: float
    cost_usd_this_turn: float
