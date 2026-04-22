"""Thin asyncpg helpers for the API layer. Uses the shared pool from
trading.common.db."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from trading.common.db import acquire


async def list_backtests(
    strategy: str | None = None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[dict]:
    query = [
        "SELECT id, strategy_name, started_at, ended_at, status, "
        "dataset_from, dataset_to, metrics, report_path FROM research.backtests"
    ]
    conditions = []
    params: list = []
    if strategy:
        conditions.append(f"strategy_name = ${len(params) + 1}")
        params.append(strategy)
    if status:
        conditions.append(f"status = ${len(params) + 1}")
        params.append(status)
    if conditions:
        query.append("WHERE " + " AND ".join(conditions))
    query.append(
        f"ORDER BY started_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    )
    params.extend([limit, offset])
    sql = " ".join(query)
    async with acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def get_backtest(backtest_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM research.backtests WHERE id = $1", uuid.UUID(backtest_id)
        )
    return dict(row) if row else None


async def backtest_trades(backtest_id: str, limit: int = 100) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT trade_idx, instrument, strategy_side, entry_ts, entry_price, "
            "exit_ts, exit_price, pnl, fees, edge_bps, vol_regime "
            "FROM research.backtest_trades WHERE backtest_id = $1 "
            "ORDER BY trade_idx ASC LIMIT $2",
            uuid.UUID(backtest_id), limit,
        )
    return [dict(r) for r in rows]


async def create_job(payload: dict, requested_by: str) -> str:
    job_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research.backtest_jobs
                (id, status, strategy_name, params_file, data_source,
                 from_ts, to_ts, slug_encodes_open_ts, polybot_db_path,
                 requested_by)
            VALUES ($1,'queued',$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            job_id,
            payload["strategy"],
            payload["params_file"],
            payload["source"],
            payload["from_ts"],
            payload["to_ts"],
            payload["slug_encodes_open_ts"],
            payload.get("polybot_db"),
            requested_by,
        )
    return job_id


async def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = list(fields.keys())
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    sql = f"UPDATE research.backtest_jobs SET {sets} WHERE id = $1"
    async with acquire() as conn:
        await conn.execute(sql, uuid.UUID(job_id), *[fields[c] for c in cols])


async def get_job(job_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM research.backtest_jobs WHERE id = $1", uuid.UUID(job_id)
        )
    return dict(row) if row else None


async def list_strategies() -> list[dict]:
    """Read active strategies from staging.toml registry + their paused state."""
    from pathlib import Path

    import tomli

    cfg = tomli.loads(Path("config/environments/staging.toml").read_text())
    out: list[dict] = []
    async with acquire() as conn:
        for name, entry in cfg.get("strategies", {}).items():
            row = await conn.fetchrow(
                "SELECT state, updated_at FROM trading.strategy_state WHERE strategy_id = $1",
                name,
            )
            state = json.loads(row["state"]) if row and row["state"] else {}
            out.append(
                {
                    "name": name,
                    "enabled": bool(entry.get("enabled")),
                    "paused": bool(state.get("paused", False)),
                    "updated_at": row["updated_at"] if row else None,
                    "params_file": entry.get("params_file"),
                }
            )
    return out


async def set_strategy_pause(name: str, paused: bool, by: str) -> None:
    state = {"paused": paused, "by": by, "ts": datetime.now(tz=UTC).timestamp()}
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO trading.strategy_state (strategy_id, mode, state, updated_at)
            VALUES ($1, 'paper', $2::jsonb, now())
            ON CONFLICT (strategy_id) DO UPDATE
              SET state = $2::jsonb, updated_at = now()
            """,
            name,
            json.dumps(state),
        )


async def recent_trades(n: int, strategy: str | None) -> list[dict]:
    query = (
        "SELECT o.ts_submit, o.strategy_id, o.instrument_id, o.price AS entry_price, "
        "f_exit.price AS exit_price, "
        "(f_exit.metadata::jsonb->>'pnl')::numeric AS pnl, "
        "(f_exit.metadata::jsonb->>'resolution') AS resolution "
        "FROM trading.orders o "
        "LEFT JOIN trading.fills f_exit "
        "  ON f_exit.order_id = o.order_id "
        "  AND f_exit.metadata::jsonb->>'kind' = 'settle' "
        "WHERE o.mode = 'paper'"
    )
    params: list = []
    if strategy:
        query += " AND o.strategy_id = $1"
        params.append(strategy)
    query += f" ORDER BY o.ts_submit DESC LIMIT {int(n)}"
    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def pnl_in_period(from_dt, to_dt, strategy: str | None) -> dict:
    query = (
        "SELECT COALESCE(SUM((f.metadata::jsonb->>'pnl')::numeric), 0) AS pnl, "
        "COUNT(*) FILTER (WHERE f.metadata::jsonb->>'kind' = 'settle') AS n_trades "
        "FROM trading.fills f "
        "JOIN trading.orders o ON o.order_id = f.order_id "
        "WHERE f.mode = 'paper' AND f.ts >= $1 AND f.ts < $2"
    )
    params: list = [from_dt, to_dt]
    if strategy:
        query += " AND o.strategy_id = $3"
        params.append(strategy)
    async with acquire() as conn:
        row = await conn.fetchrow(query, *params)
    return {"pnl": float(row["pnl"] or 0.0), "n_trades": int(row["n_trades"] or 0)}


async def open_positions(strategy: str | None) -> list[dict]:
    query = (
        "SELECT strategy_id, instrument_id, qty, avg_price, realized_pnl, "
        "unrealized_pnl, ts "
        "FROM trading.positions_snapshots "
        "WHERE mode = 'paper' "
    )
    params: list = []
    if strategy:
        query += " AND strategy_id = $1"
        params.append(strategy)
    query += " ORDER BY ts DESC LIMIT 100"
    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]
