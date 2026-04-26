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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from trading.api import db as apidb
from trading.api.auth import require_token
from trading.api.models import (
    JobStatus,
    KillswitchRequest,
    KillswitchResponse,
    LLMChatRequest,
    LLMChatResponse,
    NewBacktestRequest,
    PauseResponse,
    RestartServiceRequest,
    RestartServiceResponse,
)
from trading.api.service_control import (
    ServiceControlError,
)
from trading.api.service_control import (
    restart_service as restart_docker_service,
)
from trading.api.worker import run_job
from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger
from trading.llm.client import LLMError, LLMPolicyError
from trading.llm.orchestrator import run_turn
from trading.llm.rate_limit import RateLimitError

configure_logging()
log = get_logger("api")

TEMPLATES = Jinja2Templates(directory="src/trading/api/templates")
DASHBOARD_DIR = Path("src/trading/api/dashboard").resolve()

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


async def _status_payload() -> dict:
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
    return await _status_payload()


@app.get("/health", dependencies=[Depends(require_token)])
@app.get("/api/v1/health", dependencies=[Depends(require_token)])
async def api_health():
    status = await _status_payload()
    return {
        "ok": bool(status.get("engine_up")) and not bool(status.get("kill_switch_active")),
        **status,
    }


@app.get("/api/v1/positions", dependencies=[Depends(require_token)])
async def api_positions(strategy: str | None = None):
    return {"positions": await apidb.open_positions(strategy)}


@app.get("/api/v1/trades/recent", dependencies=[Depends(require_token)])
async def api_recent_trades(n: int = 5, strategy: str | None = None):
    n = max(1, min(50, n))
    return {"trades": await apidb.recent_trades(n, strategy)}


@app.get("/trades/recent", dependencies=[Depends(require_token)])
async def api_recent_trades_compat(limit: int = 5, strategy: str | None = None):
    return await api_recent_trades(n=limit, strategy=strategy)


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


@app.get("/metrics/pnl", dependencies=[Depends(require_token)])
@app.get("/api/v1/metrics/pnl", dependencies=[Depends(require_token)])
async def api_pnl_metrics(period: str = "today", strategy: str | None = None):
    return await api_pnl(period=period, strategy=strategy)


@app.post(
    "/system/restart",
    response_model=RestartServiceResponse,
    dependencies=[Depends(require_token)],
)
@app.post(
    "/api/v1/system/restart",
    response_model=RestartServiceResponse,
    dependencies=[Depends(require_token)],
)
async def api_restart_service(req: RestartServiceRequest):
    try:
        resolved = await restart_docker_service(req.service)
    except ServiceControlError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    log.warning(
        "api.system.restart",
        requested_service=resolved.requested_name,
        container_name=resolved.container_name,
    )
    return RestartServiceResponse(
        requested_service=resolved.requested_name,
        container_name=resolved.container_name,
        status="restarted",
        detail="restart requested via Docker API",
        restarted_at=datetime.now(tz=UTC),
    )


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


# ------------------------------------------------------------------- LLM


def _user_id_for_request(request: Request) -> str:
    """Derive a stable user_id for the rate limit + conversation tables.

    We reuse the cookie or header token, hashed, so multiple browsers
    that share the same TEA_API_TOKEN map to the same namespace.
    """
    import hashlib

    presented = request.headers.get("X-TEA-Token") or request.cookies.get("tea_token") or ""
    if presented:
        return "web:" + hashlib.sha256(presented.encode()).hexdigest()[:8]
    return "web:anon"


@app.post(
    "/api/v1/llm/chat",
    response_model=LLMChatResponse,
    dependencies=[Depends(require_token)],
)
async def api_llm_chat(req: LLMChatRequest, request: Request) -> LLMChatResponse:
    if not req.session_id.strip():
        raise HTTPException(status_code=400, detail="session_id required")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message required")

    user_id = _user_id_for_request(request)
    try:
        turn = await run_turn(
            session_id=req.session_id,
            user_id=user_id,
            message=req.message,
            context_refs=[r.model_dump() for r in req.context_refs],
            model=req.model,
        )
    except RateLimitError as e:
        headers = {}
        if e.retry_after_s is not None:
            headers["Retry-After"] = str(e.retry_after_s)
        raise HTTPException(status_code=e.status_code, detail=e.reason, headers=headers) from e
    except LLMPolicyError as e:
        log.error("api.llm.policy_violation", err=str(e))
        raise HTTPException(status_code=502, detail="provider returned disallowed payload") from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    conv = turn.conversation
    return LLMChatResponse(
        session_id=conv.session_id,
        assistant=turn.assistant_content,
        model=conv.model,
        tokens_in_total=conv.tokens_in,
        tokens_out_total=conv.tokens_out,
        cost_usd_total=conv.cost_usd,
        cost_usd_this_turn=turn.chat_result.cost_usd,
    )


@app.post("/api/v1/llm/reset", dependencies=[Depends(require_token)])
async def api_llm_reset(session_id: str):
    from trading.common.db import acquire as _acquire

    async with _acquire() as conn:
        await conn.execute(
            "DELETE FROM research.llm_conversations WHERE session_id = $1",
            session_id,
        )
    log.info("api.llm.reset", session_id=session_id)
    return {"session_id": session_id, "deleted": True}


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


@app.get("/research/chat", response_class=HTMLResponse, include_in_schema=False)
async def research_chat(request: Request, session_id: str | None = None):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    import uuid as _uuid

    from trading.llm.client import whitelist_ids
    from trading.llm.store import get_by_session as _get_conv

    sid = session_id or f"web-{_uuid.uuid4().hex[:10]}"
    conv = await _get_conv(sid)
    backtests = await apidb.list_backtests(limit=20, status="completed")
    strategies = await apidb.list_strategies()
    return TEMPLATES.TemplateResponse(
        request,
        "research_chat.html",
        {
            "session_id": sid,
            "conversation": conv,
            "backtests": [_backtest_row_to_dict(r) for r in backtests],
            "strategies": strategies,
            "models": whitelist_ids(),
            "default_model": get_settings().llm_default_model,
        },
    )


# --------------------------------------------------- JSON: dashboard view-model


@app.get("/api/v1/dashboard/overview", dependencies=[Depends(require_token)])
async def api_dashboard_overview():
    """Aggregated view-model for the React dashboard.

    Returns engine state, per-strategy live metrics (24h+7d PnL, n_trades,
    rolling win rate from last 50 settles), recent trades and a slice of
    backtests in one round-trip so the SPA can render without N requests.
    """
    status = await _status_payload()
    now = datetime.now(tz=UTC)
    last_24h_start = now - timedelta(hours=24)
    last_7d_start = now - timedelta(days=7)

    strategies_aug: list[dict] = []
    pnl_24h_total = 0.0
    pnl_7d_total = 0.0
    n_trades_24h_total = 0

    for s in status["strategies"]:
        name = s["name"]
        p24 = await apidb.pnl_in_period(last_24h_start, now, name)
        p7d = await apidb.pnl_in_period(last_7d_start, now, name)
        # Rolling win rate from last 50 settled trades.
        recent = await apidb.recent_trades(50, name)
        pnls = [float(t["pnl"]) for t in recent if t.get("pnl") is not None]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = (wins / len(pnls)) if pnls else 0.0
        strategies_aug.append(
            {
                "id": name,
                "name": name,
                "label": name,
                "venue": "Polymarket",
                "asset": "BTC-5min",
                "status": "paused" if s["paused"] else "running",
                "enabled": s["enabled"],
                "paused": s["paused"],
                "pnl_24h": float(p24["pnl"]),
                "pnl_7d": float(p7d["pnl"]),
                "n_trades_24h": int(p24["n_trades"]),
                "win_rate": win_rate,
                # Sharpe / MDD / heartbeat / horizon are not currently
                # exposed via the API; the dashboard renders 0 / "—" for
                # these. Wiring them requires research.strategy_health
                # rollups or a per-strategy heartbeat key in Redis.
                "sharpe": 0.0,
                "mdd": 0.0,
                "horizon_s": 300,
                "last_signal_s": 0,
                "heartbeat_ms": 0,
            }
        )
        pnl_24h_total += float(p24["pnl"])
        pnl_7d_total += float(p7d["pnl"])
        n_trades_24h_total += int(p24["n_trades"])

    recent_all = await apidb.recent_trades(20, None)

    def _trade_to_dict(t: dict) -> dict:
        ts = t.get("ts_submit")
        pnl = t.get("pnl")
        entry = t.get("entry_price")
        exit_ = t.get("exit_price")
        return {
            "t": ts.strftime("%H:%M:%S") if ts else "",
            "strat": t.get("strategy_id") or "",
            "side": "BUY" if (entry is not None and exit_ is not None and float(exit_) >= float(entry)) else "SELL",
            "venue": "Polymarket",
            "sym": t.get("instrument_id") or "",
            "px": f"{float(entry):.4f}" if entry is not None else "",
            "qty": 1,
            "pnl": float(pnl) if pnl is not None else 0.0,
        }

    backtests = await apidb.list_backtests(limit=50)
    return {
        "engine": {
            "up": bool(status["engine_up"]),
            "kill_switch_active": bool(status["kill_switch_active"]),
            "heartbeat_age_s": status["heartbeat_age_s"],
        },
        "strategies": strategies_aug,
        "totals": {
            "pnl_24h": pnl_24h_total,
            "pnl_7d": pnl_7d_total,
            "n_trades_24h": n_trades_24h_total,
        },
        "recent_trades": [_trade_to_dict(t) for t in recent_all],
        "backtests": [_backtest_row_to_dict(r) for r in backtests],
    }


# --------------------------------------------------------- HTML: dashboard (React)


@app.get("/dashboard", include_in_schema=False)
async def dashboard_root(request: Request):
    # Trailing slash so relative asset URLs (src/*.jsx, data/*.json) resolve
    # under /dashboard/ instead of /.
    return RedirectResponse("/dashboard/", status_code=308)


@app.get("/dashboard/", include_in_schema=False)
@app.get("/dashboard/{subpath:path}", include_in_schema=False)
async def dashboard_static(request: Request, subpath: str = ""):
    redir = _cookie_auth_or_redirect(request)
    if redir:
        return redir
    if not subpath or subpath.endswith("/"):
        target = DASHBOARD_DIR / "index.html"
    else:
        target = (DASHBOARD_DIR / subpath).resolve()
        try:
            target.relative_to(DASHBOARD_DIR)
        except ValueError as e:
            raise HTTPException(status_code=403, detail="forbidden") from e
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


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
