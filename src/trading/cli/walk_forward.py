"""Walk-forward CLI — runs the rolling split analysis and persists a
walk_forward_runs row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import tomli

from trading.common.config import get_settings
from trading.common.logging import configure_logging, get_logger
from trading.engine.backtest_driver import EntryWindowConfig, FillConfig, IndicatorConfig
from trading.engine.data_loader import PolybotSQLiteLoader
from trading.engine.walk_forward import run_walk_forward, walk_forward_to_dict
from trading.research.report import _params_hash

log = get_logger("cli.walk_forward")


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _load_strategy(name: str, cfg: dict):
    if name == "polymarket_btc5m/imbalance_v3":
        from trading.strategies.polymarket_btc5m.imbalance_v3 import ImbalanceV3

        return lambda: ImbalanceV3(config=cfg)
    raise SystemExit(f"unknown strategy: {name}")


async def _persist(row: dict, dsn: str) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            """
            INSERT INTO research.walk_forward_runs
                (id, strategy_name, params_hash, started_at, ended_at, status,
                 verdict, splits, summary)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb)
            """,
            row["id"],
            row["strategy_name"],
            row["params_hash"],
            row["started_at"],
            row["ended_at"],
            "completed",
            row["verdict"],
            json.dumps(row["splits"]),
            json.dumps(row["summary"]),
        )
    finally:
        await conn.close()


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.walk_forward")
    p.add_argument("--strategy", required=True)
    p.add_argument("--params", required=True)
    p.add_argument("--from", dest="from_ts", required=True, type=_parse_ts)
    p.add_argument("--to", dest="to_ts", required=True, type=_parse_ts)
    p.add_argument("--train-days", type=int, default=6)
    p.add_argument("--test-days", type=int, default=2)
    p.add_argument("--step-days", type=int, default=1)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--polybot-db", default="/polybot-btc5m-data/polybot.db")
    p.add_argument("--tolerance", type=float, default=0.30)
    p.add_argument("--out", default=None, help="optional JSON output path")
    args = p.parse_args()

    cfg = tomli.loads(Path(args.params).read_text())
    factory = _load_strategy(args.strategy, cfg)
    loader = PolybotSQLiteLoader(args.polybot_db)

    started_at = datetime.now(tz=UTC)
    result = run_walk_forward(
        strategy_factory=factory,
        loader=loader,
        from_dt=args.from_ts,
        to_dt=args.to_ts,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        stake_usd=min(
            float(cfg.get("sizing", {}).get("stake_usd", 3.0)),
            float(cfg.get("risk", {}).get("max_position_size_usd", 5.0)),
        ),
        fill_cfg=FillConfig(
            slippage_bps=float(cfg["fill_model"]["slippage_bps"]),
            fill_probability=float(cfg["fill_model"]["fill_probability"]),
        ),
        entry_window=EntryWindowConfig(
            earliest_entry_t_s=int(cfg["backtest"]["earliest_entry_t_s"]),
            latest_entry_t_s=int(cfg["backtest"]["latest_entry_t_s"]),
        ),
        risk_cfg=cfg["risk"],
        indicator_cfg=IndicatorConfig(),
        config_used=cfg,
        seed=args.seed,
        tolerance=args.tolerance,
    )
    payload = walk_forward_to_dict(result)
    log.info(
        "walk_forward.done",
        strategy=args.strategy,
        verdict=result.verdict,
        splits=len(result.splits),
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2, default=str))
        log.info("walk_forward.saved", path=args.out)

    row = {
        "id": str(uuid.uuid4()),
        "strategy_name": args.strategy,
        "params_hash": _params_hash(cfg),
        "started_at": started_at,
        "ended_at": datetime.now(tz=UTC),
        "verdict": result.verdict,
        "splits": payload["splits"],
        "summary": payload["summary"],
    }
    asyncio.run(_persist(row, get_settings().pg_dsn))
    log.info("walk_forward.persisted", id=row["id"])


if __name__ == "__main__":
    main()
