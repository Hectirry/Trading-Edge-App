"""FastAPI app — auth-gated JSON API + server-rendered dashboard.

Routes:
  /api/v1/*      JSON endpoints (token-guarded)
  /research/*    HTML dashboard (cookie or token)
  /login         token submit form

See ADR 0009 for architecture.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from trading.api import db as apidb
from trading.api.auth import require_token
from trading.api.models import (
    JobStatus,
    KillswitchRequest,
    KillswitchResponse,
    NewBacktestRequest,
    PauseResponse,
)
from trading.api.worker import run_job
from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger

configure_logging()
log = get_logger("api")

TEMPLATES = Jinja2Templates(directory="src/trading/api/templates")

app = FastAPI(title="TEA API", version="0.4.0")

KILL_SWITCH_API = "/var/tea/control/KILL_SWITCH"


def _redis_url() -> str:
    s = get_settings()
    return f"redis://{s.redis_host}:{s.redis_port}/0"


# ---------------------------------------------------------------- auth helpers


def _cookie_auth_or_redirect(request: Request):
    tok = request.cookies.get("tea_token")
    if not tok or tok != get_settings().api_token:
        return RedirectResponse("/login", status_code=303)
    return None


# -------------------------------------------------------------- JSON: backtests


@app.get("/api/v1/backtests", dependencies=[Depends(require_token)])
async def api_list_backtests(
    strategy: str | None = None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
):
    rows = await apidb.list_backtests(strategy=strategy, limit=limit, offset=offset, status=status)
    return {"backtests": [_backtest_row_to_dict(r) for r in rows], "limit": limit, "offset": offset}


@app.get("/api/v1/backtests/{backtest_id}", dependencies=[Depends(require_token)])
async def api_get_backtest(backtest_id: str):
    row = await apidb.get_backtest(backtest_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return _backtest_row_to_dict(row)


@app.get("/api/v1/backtests/{backtest_id}/trades", dependencies=[Depends(require_token)])
async def api_backtest_trades(backtest_id: str, limit: int = 100):
    rows = await apidb.backtest_trades(backtest_id, limit=limit)
    return {"trades": rows, "limit": limit}


@app.post("/api/v1/backtests", dependencies=[Depends(require_token)])
async def api_run_backtest(req: NewBacktestRequest, request: Request):
    requested_by = req.requested_by or f"web:{request.client.host if request.client else '-'}"
    payload = {
        "strategy": req.strategy,
        "params_file": req.params_file,
        "source": req.source,
        "from_ts": req.from_ts,
        "to_ts": req.to_ts,
        "slug_encodes_open_ts": req.slug_encodes_open_ts,
        "polybot_db": req.polybot_db,
    }
    job_id = await apidb.create_job(payload, requested_by=requested_by)
    asyncio.create_task(run_job(job_id))
    log.info("api.backtest.submitted", job_id=job_id, strategy=req.strategy)
    return {"job_id": job_id}


@app.get("/api/v1/jobs/{job_id}", response_model=JobStatus, dependencies=[Depends(require_token)])
async def api_get_job(job_id: str):
    row = await apidb.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JobStatus(**{k: v for k, v in row.items() if k in JobStatus.model_fields})


# ------------------------------------------------- JSON: strategy pause/resume


@app.get("/api/v1/strategies", dependencies=[Depends(require_token)])
async def api_list_strategies():
    return {"strategies": await apidb.list_strategies()}


@app.post(
    "/api/v1/strategies/{name}/pause",
    response_model=PauseResponse,
    dependencies=[Depends(require_token)],
)
async def api_pause(name: str, request: Request):
    await apidb.set_strategy_pause(name, paused=True, by="api")
    r = redis.from_url(_redis_url(), decode_responses=False)
    await r.publish(f"tea:control:{name}", b'{"action":"pause"}')
    log.info("api.strategy.pause", name=name)
    return PauseResponse(strategy=name, paused=True, by="api")


@app.post(
    "/api/v1/strategies/{name}/resume",
    response_model=PauseResponse,
    dependencies=[Depends(require_token)],
)
async def api_resume(name: str, request: Request):
    await apidb.set_strategy_pause(name, paused=False, by="api")
    r = redis.from_url(_redis_url(), decode_responses=False)
    await r.publish(f"tea:control:{name}", b'{"action":"resume"}')
    log.info("api.strategy.resume", name=name)
    return PauseResponse(strategy=name, paused=False, by="api")


# ---------------------------------------------------- JSON: status / positions


@app.get("/api/v1/status", dependencies=[Depends(require_token)])
async def api_status():
    r = redis.from_url(_redis_url(), decode_responses=False)
    hb = await r.get("tea:engine:last_heartbeat")
    import json as _json

    data = _json.loads(hb) if hb else None
    age = None
    if data:
        age = datetime.now(tz=UTC).timestamp() - float(data.get("ts", 0))
    strategies = await apidb.list_strategies()
    today = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    pnl_today = await apidb.pnl_in_period(today, today + timedelta(days=1), strategy=None)
    return {
        "engine_up": age is not None and age < 60,
        "heartbeat_age_s": age,
        "strategies": strategies,
        "pnl_today": pnl_today,
        "kill_switch_active": _kill_switch_active(),
    }


@app.get("/api/v1/positions", dependencies=[Depends(require_token)])
async def api_positions(strategy: str | None = None):
    return {"positions": await apidb.open_positions(strategy)}


@app.get("/api/v1/trades/recent", dependencies=[Depends(require_token)])
async def api_recent_trades(n: int = 5, strategy: str | None = None):
    n = max(1, min(50, n))
    return {"trades": await apidb.recent_trades(n, strategy)}


@app.get("/api/v1/pnl", dependencies=[Depends(require_token)])
async def api_pnl(period: str = "today", strategy: str | None = None):
    now = datetime.now(tz=UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        since = today
        until = today + timedelta(days=1)
    elif period == "semana":
        since = today - timedelta(days=today.weekday())
        until = since + timedelta(days=7)
    elif period == "mes":
        since = today.replace(day=1)
        next_month = (since.replace(day=28) + timedelta(days=4)).replace(day=1)
        until = next_month
    else:
        raise HTTPException(status_code=400, detail="period must be today|semana|mes")
    data = await apidb.pnl_in_period(since, until, strategy)
    return {"period": period, "from": since, "to": until, "strategy": strategy, **data}


# ---------------------------------------------------------------- killswitch


def _kill_switch_active() -> bool:
    return any(os.path.exists(p) for p in ("/etc/trading-system/KILL_SWITCH", KILL_SWITCH_API))


@app.post(
    "/api/v1/killswitch",
    response_model=KillswitchResponse,
    dependencies=[Depends(require_token)],
)
async def api_killswitch_on(req: KillswitchRequest):
    if req.confirm.strip().lower() != "sí lo entiendo":
        raise HTTPException(status_code=400, detail="confirmation phrase mismatch")
    Path(KILL_SWITCH_API).parent.mkdir(parents=True, exist_ok=True)
    Path(KILL_SWITCH_API).write_text(f"armed_by_api at {datetime.now(tz=UTC).isoformat()}\n")
    log.warning("api.killswitch.armed", path=KILL_SWITCH_API)
    return KillswitchResponse(active=True, path=KILL_SWITCH_API, at=datetime.now(tz=UTC))


@app.post(
    "/api/v1/killswitch_off",
    response_model=KillswitchResponse,
    dependencies=[Depends(require_token)],
)
async def api_killswitch_off():
    p = Path(KILL_SWITCH_API)
    if p.exists():
        p.unlink()
    log.warning("api.killswitch.disarmed", path=KILL_SWITCH_API)
    return KillswitchResponse(
        active=_kill_switch_active(),
        path=KILL_SWITCH_API,
        at=datetime.now(tz=UTC),
    )


# --------------------------------------------------------- HTML: dashboard


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/research", status_code=302)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {})


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    form = await request.form()
    token = form.get("token", "")
    if token != get_settings().api_token:
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": "invalid token"}, status_code=401
        )
    resp = RedirectResponse("/research", status_code=303)
    resp.set_cookie(
        "tea_token", token, max_age=60 * 60 * 24 * 7, httponly=True, secure=True, samesite="strict"
    )
    return resp


@app.get("/research", response_class=HTMLResponse, include_in_schema=False)
async def research_index(request: Request, strategy: str | None = None, status: str | None = None):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    backtests = await apidb.list_backtests(strategy=strategy, limit=50, status=status)
    strategies = await apidb.list_strategies()
    return TEMPLATES.TemplateResponse(
        request,
        "research_index.html",
        {
            "backtests": [_backtest_row_to_dict(r) for r in backtests],
            "strategies": strategies,
            "selected_strategy": strategy,
            "selected_status": status,
        },
    )


@app.get("/research/new", response_class=HTMLResponse, include_in_schema=False)
async def research_new(request: Request):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    strategies = await apidb.list_strategies()
    return TEMPLATES.TemplateResponse(request, "research_new.html", {"strategies": strategies})


@app.get("/research/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def research_job(request: Request, job_id: str):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    row = await apidb.get_job(job_id)
    if row is None:
        return HTMLResponse("job not found", status_code=404)
    return TEMPLATES.TemplateResponse(request, "research_job.html", {"job": row})


@app.get("/research/{backtest_id}", response_class=HTMLResponse, include_in_schema=False)
async def research_detail(request: Request, backtest_id: str):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    row = await apidb.get_backtest(backtest_id)
    if row is None:
        return HTMLResponse("backtest not found", status_code=404)
    trades = await apidb.backtest_trades(backtest_id, limit=50)
    return TEMPLATES.TemplateResponse(
        request,
        "research_detail.html",
        {"bt": _backtest_row_to_dict(row), "trades": trades},
    )


@app.get("/research-compare", response_class=HTMLResponse, include_in_schema=False)
async def research_compare(request: Request, ids: str = ""):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    id_list = [i for i in ids.split(",") if i][:3]
    runs = []
    for bid in id_list:
        row = await apidb.get_backtest(bid)
        if row is not None:
            runs.append(_backtest_row_to_dict(row))
    return TEMPLATES.TemplateResponse(request, "research_compare.html", {"runs": runs})


# --------------------------------------------------------------- helpers


def _backtest_row_to_dict(row: dict) -> dict:
    metrics = row.get("metrics") or {}
    if isinstance(metrics, str):
        import json as _json

        metrics = _json.loads(metrics)
    perf = (metrics or {}).get("performance") or {}
    risk = (metrics or {}).get("risk_adjusted") or {}
    return {
        "id": str(row["id"]),
        "strategy_name": row["strategy_name"],
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "status": row["status"],
        "dataset_from": row["dataset_from"],
        "dataset_to": row["dataset_to"],
        "n_trades": perf.get("n_trades"),
        "total_pnl": perf.get("total_pnl"),
        "win_rate": perf.get("win_rate"),
        "sharpe_per_trade": risk.get("sharpe_per_trade"),
        "mdd_usd": risk.get("mdd_usd"),
        "report_path": row.get("report_path"),
    }
