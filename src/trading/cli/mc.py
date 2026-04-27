"""Monte Carlo CLI.

Two kinds of MC, both keyed off a strategy + dataset window:

  bootstrap  — resamples the realized trade vector with replacement.
               Permutation test under a coin-flip null. Cheap (~seconds).

  block      — re-runs the backtest driver against bootstrap-resampled
               5-min Polymarket markets. Expensive (n_iter × backtest).

Usage:
  python -m trading.cli.mc \\
    --strategy polymarket_btc5m/last_90s_forecaster_v3 \\
    --params config/strategies/pbt5m_last_90s_forecaster_v3.toml \\
    --from 2026-04-01T00:00:00Z --to 2026-04-20T00:00:00Z \\
    --source polybot_sqlite --polybot-db /polybot-btc5m-data/polybot.db \\
    --kind both --n-iter 1000 --seed 42

See `src/trading/research/monte_carlo.py` for the algorithm.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tomllib as tomli
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger
from trading.engine.backtest_driver import (
    EntryWindowConfig,
    FillConfig,
    run_backtest,
)
from trading.engine.data_loader import PolybotSQLiteLoader, warn_if_polybot_stale
from trading.engine.risk import RiskManager
from trading.paper.backtest_loader import PaperTicksLoader
from trading.research.monte_carlo import (
    block_bootstrap_replay,
    block_bootstrap_to_dict,
    bootstrap_metrics,
    trade_bootstrap_to_dict,
    verdict_from_bootstrap,
)
from trading.strategies.polymarket_btc5m._macro_provider import Candle, FixedMacroProvider

log = get_logger("cli.mc")


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _params_hash(params: dict) -> str:
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


async def _load_macro_provider(*, from_ts: datetime, to_ts: datetime) -> FixedMacroProvider:
    lead_in = from_ts.timestamp() - 34 * 300
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, high, low, close FROM market_data.crypto_ohlcv "
            "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m' "
            "AND ts >= to_timestamp($1) AND ts <= $2 ORDER BY ts",
            lead_in,
            to_ts,
        )
    candles = [
        Candle(
            ts=r["ts"].timestamp(),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )
        for r in rows
    ]
    return FixedMacroProvider(candles=candles)


async def _build_factory(name: str, config: dict, macro_provider):
    """Return a sync callable that yields a fresh strategy instance.

    Heavy artifacts (model runners, macro provider) load ONCE here so
    that the inner block-bootstrap loop stays cheap. The strategy
    instance itself is reconstructed per replicate so on_start state
    cannot leak between replicas.
    """
    if name == "polymarket_btc5m/trend_confirm_t1_v1":
        from trading.strategies.polymarket_btc5m.trend_confirm_t1_v1 import (
            TrendConfirmT1V1,
        )

        return lambda: TrendConfirmT1V1(config=config)
    if name == "polymarket_btc5m/oracle_lag_v1":
        from trading.cli.backtest import _load_oracle_lag_cesta
        from trading.strategies.polymarket_btc5m.oracle_lag_v1 import OracleLagV1

        cesta = await _load_oracle_lag_cesta(config)
        return lambda: OracleLagV1(config=config, cesta=cesta)
    if name == "polymarket_btc5m/last_90s_forecaster_v3":
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            Last90sForecasterV3,
        )
        from trading.strategies.polymarket_btc5m.last_90s_forecaster_v3 import (
            load_runner_async as v3_load_runner_async,
        )

        runner = await v3_load_runner_async()
        return lambda: Last90sForecasterV3(config, macro_provider=macro_provider, model=runner)
    if name == "polymarket_btc5m/bb_residual_ofi_v1":
        from trading.strategies.polymarket_btc5m.bb_residual_ofi_v1 import (
            BBResidualOFIV1,
        )
        from trading.strategies.polymarket_btc5m.bb_residual_ofi_v1 import (
            load_runner_async as bb_ofi_load_runner_async,
        )

        runner = await bb_ofi_load_runner_async()
        return lambda: BBResidualOFIV1(config, model=runner)
    raise SystemExit(f"unknown strategy: {name}")


def _build_loader(args: argparse.Namespace):
    if args.source == "polybot_sqlite":
        warn_if_polybot_stale(args.polybot_db, expected_window_end_ts=args.to_ts.timestamp())
        return PolybotSQLiteLoader(
            db_path=args.polybot_db,
            slug_encodes_open_ts=args.slug_encodes_open_ts,
        )
    if args.source == "paper_ticks":
        return PaperTicksLoader(dsn=get_settings().pg_dsn)
    raise SystemExit(f"source not supported: {args.source}")


async def _persist(
    *,
    dsn: str,
    strategy: str,
    params_hash: str,
    backtest_id: str | None,
    kind: str,
    payload: dict,
    args: argparse.Namespace,
    started: datetime,
    verdict: str | None,
) -> str:
    run_id = str(uuid.uuid4())
    conn = await asyncpg.connect(dsn=dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO research.mc_runs
                    (id, backtest_id, strategy_name, params_hash, kind,
                     n_iter, seed, dataset_from, dataset_to,
                     started_at, ended_at, status, verdict,
                     realized, percentiles, means, stds,
                     permutation_pvalue, replicates, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                        $14::jsonb,$15::jsonb,$16::jsonb,$17::jsonb,
                        $18,$19::jsonb,$20::jsonb)
                """,
                run_id,
                backtest_id,
                strategy,
                params_hash,
                kind,
                int(payload.get("n_iter", args.n_iter)),
                int(payload.get("seed", args.seed)),
                args.from_ts,
                args.to_ts,
                started,
                datetime.now(tz=UTC),
                "completed",
                verdict,
                json.dumps(payload.get("realized", {})),
                json.dumps(payload.get("percentiles", {})),
                json.dumps(payload.get("means", {})),
                json.dumps(payload.get("stds", {})),
                payload.get("permutation_pvalue"),
                json.dumps(payload.get("replicates", [])) if payload.get("replicates") else None,
                json.dumps(
                    {
                        "source": args.source,
                        "polybot_db": args.polybot_db,
                        "slug_encodes_open_ts": bool(args.slug_encodes_open_ts),
                    }
                ),
            )
    finally:
        await conn.close()
    return run_id


async def _run(args: argparse.Namespace) -> None:
    cfg = tomli.loads(Path(args.params).read_text())
    macro_provider = await _load_macro_provider(from_ts=args.from_ts, to_ts=args.to_ts)
    factory = await _build_factory(args.strategy, cfg, macro_provider)
    loader = _build_loader(args)

    sizing = cfg.get("sizing", {})
    bt = cfg.get("backtest", {})
    fm = cfg.get("fill_model", {})
    fill_cfg = FillConfig(
        slippage_bps=float(fm.get("slippage_bps", 10.0)),
        fill_probability=float(fm.get("fill_probability", 0.95)),
        apply_fee_in_backtest=bool(fm.get("apply_fee_in_backtest", False)),
        fee_k=float(fm.get("fee_k", 0.05)),
    )
    entry_window = EntryWindowConfig(
        earliest_entry_t_s=int(bt.get("earliest_entry_t_s", 120)),
        latest_entry_t_s=int(bt.get("latest_entry_t_s", 240)),
    )
    risk_cfg = cfg.get("risk", {})
    bypass_risk = bool(risk_cfg.get("bypass_in_backtest", False))
    stake = min(
        float(sizing.get("stake_usd", 3.0)),
        float(risk_cfg.get("max_position_size_usd", 5.0)),
    )

    phash = _params_hash(cfg)
    settings = get_settings()
    started = datetime.now(tz=UTC)

    run_realized = args.kind in ("bootstrap", "both")
    run_block = args.kind in ("block", "both")

    realized_result = None
    if run_realized or (run_block and not args.no_realized):
        realized_result = run_backtest(
            strategy=factory(),
            loader=loader,
            from_ts=args.from_ts.timestamp(),
            to_ts=args.to_ts.timestamp(),
            stake_usd=stake,
            fill_cfg=fill_cfg,
            entry_window=entry_window,
            risk_manager=RiskManager({"risk": risk_cfg}),
            config_used=cfg,
            seed=args.seed,
            bypass_risk=bypass_risk,
        )
        log.info(
            "mc.realized.done",
            n_markets=realized_result.n_markets,
            n_trades=realized_result.n_trades,
        )

    persisted_ids: list[str] = []

    if run_realized and realized_result is not None:
        boot = bootstrap_metrics(realized_result.trades, n_iter=args.n_iter, seed=args.seed)
        verdict = verdict_from_bootstrap(boot)
        payload = trade_bootstrap_to_dict(boot)
        log.info(
            "mc.bootstrap.done",
            n_iter=boot.n_iter,
            verdict=verdict,
            permutation_pvalue=boot.permutation_pvalue,
            p5_total_pnl=boot.percentiles.get("total_pnl", {}).get("p5"),
            p95_total_pnl=boot.percentiles.get("total_pnl", {}).get("p95"),
        )
        if not args.no_persist:
            run_id = await _persist(
                dsn=settings.pg_dsn,
                strategy=args.strategy,
                params_hash=phash,
                backtest_id=None,
                kind="bootstrap",
                payload=payload,
                args=args,
                started=started,
                verdict=verdict,
            )
            persisted_ids.append(run_id)
            log.info("mc.bootstrap.persisted", run_id=run_id)

    if run_block:
        block = block_bootstrap_replay(
            strategy_factory=factory,
            loader=loader,
            from_ts=args.from_ts.timestamp(),
            to_ts=args.to_ts.timestamp(),
            stake_usd=stake,
            fill_cfg=fill_cfg,
            entry_window=entry_window,
            risk_cfg=risk_cfg,
            config_used=cfg,
            n_iter=args.n_iter,
            seed=args.seed,
            bypass_risk=bypass_risk,
            realized=realized_result,
        )
        payload = block_bootstrap_to_dict(block)
        log.info(
            "mc.block.done",
            n_iter=block.n_iter,
            n_source_markets=block.n_source_markets,
            mean_total_pnl=block.means.get("total_pnl"),
            p5_total_pnl=block.percentiles.get("total_pnl", {}).get("p5"),
            p95_total_pnl=block.percentiles.get("total_pnl", {}).get("p95"),
        )
        if not args.no_persist:
            run_id = await _persist(
                dsn=settings.pg_dsn,
                strategy=args.strategy,
                params_hash=phash,
                backtest_id=None,
                kind="block",
                payload=payload,
                args=args,
                started=started,
                verdict=None,
            )
            persisted_ids.append(run_id)
            log.info("mc.block.persisted", run_id=run_id)

    if args.no_persist:
        log.info("mc.persist.skipped", reason="--no-persist")
    else:
        log.info("mc.done", run_ids=persisted_ids)


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.mc")
    p.add_argument("--strategy", required=True)
    p.add_argument("--params", required=True)
    p.add_argument("--from", dest="from_ts", required=True, type=_parse_ts)
    p.add_argument("--to", dest="to_ts", required=True, type=_parse_ts)
    p.add_argument(
        "--source",
        default="polybot_sqlite",
        choices=["polybot_sqlite", "paper_ticks"],
    )
    p.add_argument("--polybot-db", default="/polybot-btc5m-data/polybot.db")
    p.add_argument("--slug-encodes-open-ts", action="store_true")
    p.add_argument(
        "--kind",
        default="both",
        choices=["bootstrap", "block", "both"],
        help="bootstrap: resample trade vector. block: resample market windows.",
    )
    p.add_argument("--n-iter", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--no-realized",
        action="store_true",
        help="When --kind=block, skip running the realized backtest first.",
    )
    p.add_argument("--no-persist", action="store_true")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
