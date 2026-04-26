"""Training CLI for ``bb_residual_ofi_v1``.

Same data assembly as ``train_last90s`` (polybot SQLite ticks +
``market_data.crypto_ohlcv`` 1 m for labels +
``market_data.crypto_trades`` for microstructure +
``market_data.polymarket_prices_history`` for the real
``implied_prob_yes``) — but the feature vector is the 14-column
BB-residual OFI block from
``trading.strategies.polymarket_btc5m._bb_ofi_features.build_vector``.

Heavy I/O helpers are imported from ``train_last90s`` (the underscore
names) so we don't duplicate ~250 lines of SQL. The contract: those
helpers are pure I/O and have stable signatures (re-derived at audit
2026-04-25); the only thing this CLI does differently is which
feature builder it calls and which model name it writes to
``research.models``.

Promotion gate is the sample-size–aware ``_passes_promotion`` from
``train_last90s`` (AUC ≥ 0.55, Brier ≤ 0.245 / 0.260, ECE ≤ 0.05 /
0.20). Failing → row written ``is_active = FALSE``; the strategy
stays shadow.

Usage::

    docker compose exec tea-engine python -m trading.cli.train_bb_ofi \\
        --from 2026-03-23 --to 2026-04-21 \\
        --polybot-btc5m /polybot-btc5m-data/polybot.db \\
        --polybot-agent /polybot-btc5m-data/polybot.db \\
        --optuna-trials 80 --time-budget-s 1800 \\
        --use-real-implied-prob \\
        --promote
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Reuse pure I/O helpers from the v3 trainer. These are private (_xxx)
# but their signatures are stable; importing them avoids 250 lines of
# duplication. If they ever move, this module breaks loudly at import
# time (preferable to silent feature drift).
from trading.cli.train_last90s import (
    _fetch_ohlcv_1m_closes,
    _load_resolved_markets,
    _load_ticks_for_slug,
    _passes_promotion,
    train,
)
from trading.strategies.polymarket_btc5m._bb_ofi_features import (
    FEATURE_NAMES,
    BBOFIFeatureInputs,
    build_vector,
)

log = logging.getLogger("cli.train_bb_ofi")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class Sample:
    open_ts: float
    close_ts: float
    slug: str
    features: list[float]
    label: int


# --------------------------------------------------------------- env / dsn


def _pg_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}",
    )


# ----------------------------------------- markets / spots from Postgres


def _load_settled_markets_from_pg(
    pg_dsn: str,
    t_from: datetime,
    t_to: datetime,
) -> list[dict]:
    """All resolved BTC up/down 5 m markets in window, with open_price /
    close_price re-derived from Binance ``crypto_ohlcv`` 1 m closes
    (same canonical path as ``_load_resolved_markets`` — preferring
    drop over poison when a 1 m candle is missing at minute(open) or
    minute(close)).

    This is the engine-Postgres equivalent of polybot's SQLite path:
    no dependency on the polybot tick stream's narrow 8-day history.
    Use this when ``crypto_trades`` coverage exceeds polybot's.
    """
    import psycopg2

    t_from_unix = int(t_from.timestamp())
    t_to_unix = int(t_to.timestamp())

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT slug, condition_id,
                       EXTRACT(EPOCH FROM open_time)::bigint,
                       EXTRACT(EPOCH FROM close_time)::bigint
                FROM market_data.polymarket_markets
                WHERE slug LIKE 'btc-%%updown-5m-%%'
                  AND open_time >= to_timestamp(%s)
                  AND close_time <= to_timestamp(%s)
                ORDER BY open_time
                """,
                (t_from_unix, t_to_unix),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    minutes: set[int] = set()
    for _, _, open_ts, close_ts in rows:
        minutes.add((int(open_ts) // 60) * 60)
        minutes.add((int(close_ts) // 60) * 60)
    closes = _fetch_ohlcv_1m_closes(pg_dsn, min(minutes), max(minutes))

    out: list[dict] = []
    n_dropped_gap = 0
    for slug, cid, open_ts, close_ts in rows:
        bin_open = closes.get((int(open_ts) // 60) * 60)
        bin_close = closes.get((int(close_ts) // 60) * 60)
        if bin_open is None or bin_close is None:
            n_dropped_gap += 1
            continue
        out.append(
            {
                "slug": slug,
                "condition_id": cid,
                "open_ts": int(open_ts),
                "close_ts": int(close_ts),
                "open_price": float(bin_open),
                "close_price": float(bin_close),
            }
        )
    if n_dropped_gap:
        log.info(
            "_load_settled_markets_from_pg: dropped %d markets with OHLCV gap",
            n_dropped_gap,
        )
    log.info(
        "_load_settled_markets_from_pg: kept %d / %d markets in window",
        len(out),
        len(rows),
    )
    return out


def _batch_fetch_implied_yes(
    pg_dsn: str,
    market_open_ts: list[tuple[str, int]],
) -> dict[str, float]:
    """Batch ``_fetch_polymarket_implied_yes`` for many markets at once.

    For each (slug, open_ts), find the latest YES price ≤ open_ts+210
    (the as_of point) using a single SQL with DISTINCT ON. Returns
    dict[slug] = price; absent slug means no implied_prob found
    (caller drops sample).

    Why batch: the per-market version runs one round-trip per market;
    8 k markets × 2 s each = 5 h. The batch form does it in one PG
    plan that uses the (token_id, ts) PK efficiently.
    """
    if not market_open_ts:
        return {}
    import psycopg2

    slugs = [s for s, _ in market_open_ts]
    as_of_list = [int(o) + 210 for _, o in market_open_ts]

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH wanted AS (
                    SELECT slug, as_of FROM unnest(%s::text[], %s::bigint[]) AS t(slug, as_of)
                ),
                joined AS (
                    SELECT w.slug,
                           pp.price::float8 AS price,
                           pp.ts
                    FROM wanted w
                    JOIN market_data.polymarket_markets pm ON pm.slug = w.slug
                    JOIN market_data.polymarket_prices_history pp
                      ON pp.condition_id = pm.condition_id
                     AND pp.outcome = 'YES'
                     AND pp.ts <= to_timestamp(w.as_of)
                )
                SELECT DISTINCT ON (slug) slug, price
                FROM joined
                ORDER BY slug, ts DESC
                """,
                (slugs, as_of_list),
            )
            return {row[0]: float(row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def _load_baseline_trades_per_day(pg_dsn: str, t_from: int, t_to: int) -> dict[int, int]:
    """Trades-per-day for the BTCUSDT tape, keyed by day-floor unix
    timestamp. Used as the ``trade_intensity`` baseline so we don't
    run a COUNT(*) over crypto_trades for every market window.

    The intensity feature already smooths via
    ``trade_intensity(n_in_window, baseline_24h, window_s=90)`` which
    treats baselines < 100 as neutral; sub-day variance is below the
    threshold of relevance for this feature.
    """
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM date_trunc('day', ts))::bigint AS day, "
                "       COUNT(*)::bigint "
                "FROM market_data.crypto_trades "
                "WHERE exchange='binance' AND symbol='BTCUSDT' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "GROUP BY 1",
                (int(t_from) - 86400, int(t_to)),
            )
            return {int(d): int(n) for (d, n) in cur.fetchall()}
    finally:
        conn.close()


def _fetch_trades_in_window(
    pg_dsn: str,
    end_ts_unix: int,
    window_s: int,
) -> list[tuple[float, float, str]]:
    """Trades in [end - window_s, end]. Just the trades — baseline is
    fetched separately (per-day cache) to avoid 24h COUNT per market."""
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price::float8, qty::float8, side "
                "FROM market_data.crypto_trades "
                "WHERE exchange='binance' AND symbol='BTCUSDT' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                (end_ts_unix - window_s, end_ts_unix),
            )
            return [(float(p), float(q), str(s)) for (p, q, s) in cur.fetchall()]
    finally:
        conn.close()


def _load_spots_from_crypto_trades(
    pg_dsn: str,
    start_ts: int,
    end_ts: int,
) -> list[float]:
    """Reconstruct a 1 Hz spot stream from ``market_data.crypto_trades``
    by forward-filling: for each second in ``[start_ts, end_ts]``, the
    last trade price with ``ts ≤ sec``.

    Why: polybot SQLite ticks only cover the last ~8 days. Binance trade
    tape covers ~35 days at much higher granularity. Resampling trades
    to 1 Hz gives the same scale ``realized_vol_per_sqrt_s`` expects
    (σ of 1 s log-returns) without the polybot dependency.

    Returns empty list when no trades exist in [start-5, end].
    """
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM ts)::float8, price::float8 "
                "FROM market_data.crypto_trades "
                "WHERE exchange='binance' AND symbol='BTCUSDT' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "ORDER BY ts ASC",
                (int(start_ts) - 5, int(end_ts)),
            )
            trades = cur.fetchall()
    finally:
        conn.close()

    if not trades:
        return []

    out: list[float] = []
    idx = 0
    n = len(trades)
    # Advance idx to the last trade ≤ start_ts before emitting.
    while idx + 1 < n and trades[idx + 1][0] <= start_ts:
        idx += 1
    for sec in range(int(start_ts), int(end_ts) + 1):
        while idx + 1 < n and trades[idx + 1][0] <= sec:
            idx += 1
        if trades[idx][0] <= sec:
            out.append(float(trades[idx][1]))
        # Else: still before first trade — skip; rare at start.
    return out


# --------------------------------------------------------- sample assembly


def build_samples(
    markets: list[dict],
    *,
    sqlite_sources: list[Path],
    pg_dsn: str,
    use_real_implied_prob: bool = True,
    microstructure_window_s: int = 90,
    large_threshold_usd: float = 100_000.0,
    spots_source: str = "auto",
) -> list[Sample]:
    """Assemble Sample rows at t=210 s for each market.

    ``spots_source``:
    - ``"polybot"`` — read 1 Hz ticks from polybot SQLite. Limited by
      polybot retention (~8 d).
    - ``"crypto_trades"`` — reconstruct 1 Hz from
      ``market_data.crypto_trades`` (35 d). Recommended.
    - ``"auto"`` (default) — try polybot first, fall back to crypto_trades.

    Drop policies (prefer-drop-over-poison):
    - Spots < 60 in [open, open+210] → drop
    - Real implied_prob requested but missing → drop
    - Microstructure trades empty → drop (OFI is the strategy's
      whole point; a market without trades is a market without signal)
    - Realized vol over 90 s tail = 0 (constant series) → drop
    """
    from trading.engine.features.binance_microstructure import (
        binance_microstructure_from_trades,
    )

    # Pre-compute per-day trade counts so the per-market microstructure
    # path doesn't run a 24h COUNT(*) over crypto_trades for each call.
    if markets:
        t_min = min(int(m["open_ts"]) for m in markets)
        t_max = max(int(m["close_ts"]) for m in markets)
        baseline_by_day = _load_baseline_trades_per_day(pg_dsn, t_min, t_max)
        log.info(
            "baseline trades-per-day cache: %d days, mean %d trades/day",
            len(baseline_by_day),
            int(sum(baseline_by_day.values()) / max(1, len(baseline_by_day))),
        )
    else:
        baseline_by_day = {}

    # Batch implied_yes lookup. Single SQL handles all markets vs the
    # per-market loop (5 h → seconds).
    implied_yes_cache: dict[str, float] = {}
    if use_real_implied_prob and markets:
        implied_yes_cache = _batch_fetch_implied_yes(
            pg_dsn,
            [(m["slug"], int(m["open_ts"])) for m in markets],
        )
        log.info(
            "implied_yes cache: %d / %d markets have a price ≤ as_of",
            len(implied_yes_cache),
            len(markets),
        )

    samples: list[Sample] = []
    n_dropped_ticks = 0
    n_dropped_implied = 0
    n_dropped_micro = 0
    n_dropped_vol = 0
    n_from_polybot = 0
    n_from_trades = 0

    for i, m in enumerate(markets):
        if i and i % 500 == 0:
            log.info(
                "build_samples: progress %d/%d (kept=%d dropped tk=%d im=%d mc=%d vol=%d)",
                i,
                len(markets),
                len(samples),
                n_dropped_ticks,
                n_dropped_implied,
                n_dropped_micro,
                n_dropped_vol,
            )
        open_ts = int(m["open_ts"])
        close_ts = int(m["close_ts"])
        as_of = float(open_ts + 210)

        spots: list[float] = []
        if spots_source in ("polybot", "auto") and sqlite_sources:
            for src in sqlite_sources:
                spots = _load_ticks_for_slug(src, m["slug"], float(open_ts), as_of)
                if len(spots) >= 60:
                    n_from_polybot += 1
                    break
        if len(spots) < 60 and spots_source in ("crypto_trades", "auto"):
            spots = _load_spots_from_crypto_trades(
                pg_dsn, int(open_ts), int(as_of)
            )
            if len(spots) >= 60:
                n_from_trades += 1
        if len(spots) < 60:
            n_dropped_ticks += 1
            continue

        if use_real_implied_prob:
            implied = implied_yes_cache.get(m["slug"])
            if implied is None:
                n_dropped_implied += 1
                continue
        else:
            implied = 0.5

        trades_raw = _fetch_trades_in_window(
            pg_dsn, int(as_of), window_s=microstructure_window_s
        )
        if not trades_raw:
            n_dropped_micro += 1
            continue
        # Per-day cached baseline (within ±20 % of the true 24 h count
        # but does not require a per-market COUNT on a 10 M-row table).
        day_floor = (int(as_of) // 86400) * 86400
        baseline_24h = baseline_by_day.get(day_floor, 0)
        # Reconstruct Trade-shaped objects expected by the aggregator.
        from trading.engine.features.binance_microstructure import Trade

        trades = [Trade(price=p, qty=q, side=s) for (p, q, s) in trades_raw]
        ms_features = binance_microstructure_from_trades(
            trades=trades,
            baseline_trades_24h=baseline_24h,
            window_s=microstructure_window_s,
            large_threshold_usd=large_threshold_usd,
        )

        inputs = BBOFIFeatureInputs(
            spot_price=float(spots[-1]),
            open_price=float(m["open_price"]),
            t_in_window_s=210.0,
            spots_last_90s=spots,
            implied_prob_yes=float(implied),
            # No L2 history backfill yet — neutral defaults so the
            # model can either ignore or learn the bias of having no
            # signal here. Documented so reviewers don't miss it.
            pm_spread_bps=50.0,
            pm_imbalance=0.0,
            ms_features=ms_features,
            bb_T_seconds=300.0,
        )
        vec, debug = build_vector(inputs)

        # Defensive: if vol was 0 (constant spots), the BB prior degrades
        # to 0.5 and the model sees a degenerate sample. Drop those
        # rather than train on neutral noise.
        if debug["vol_per_sqrt_s"] <= 0.0:
            n_dropped_vol += 1
            continue

        # Label from Binance 1m closes (re-derived in _load_resolved_markets).
        label = 1 if float(m["close_price"]) > float(m["open_price"]) else 0

        samples.append(
            Sample(
                open_ts=float(open_ts),
                close_ts=float(close_ts),
                slug=m["slug"],
                features=vec,
                label=label,
            )
        )

    log.info(
        "build_samples: kept=%d (polybot=%d, trades=%d) "
        "dropped(ticks=%d, implied=%d, micro=%d, vol=%d)",
        len(samples),
        n_from_polybot,
        n_from_trades,
        n_dropped_ticks,
        n_dropped_implied,
        n_dropped_micro,
        n_dropped_vol,
    )
    return samples


# ---------------------------------------------------------- write artefacts


def write_artefacts(
    *,
    name: str,
    trained: dict,
    training_period_from: datetime,
    training_period_to: datetime,
    promote: bool,
) -> dict:
    """Mirror of train_last90s.write_artefacts but with the bb_ofi
    14-feature names and the bb_ofi model name. Kept inline rather than
    imported because the artefact path layout is name-keyed and we
    want a separate ``models/bb_residual_ofi_v1/<version>/`` tree.
    """
    import asyncio
    import json
    import pickle
    import subprocess
    import uuid

    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    version = f"bb_ofi_{stamp}"
    out_dir = Path("models") / name / version
    out_dir.mkdir(parents=True, exist_ok=True)
    trained["model"].save_model(str(out_dir / "model.lgb"))
    if trained["calibrator"] is not None:
        with open(out_dir / "calibrator.pkl", "wb") as f:
            pickle.dump(trained["calibrator"], f)

    try:
        git_sha = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[3],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        git_sha = "unknown"

    meta = {
        "name": name,
        "version": version,
        "feature_names": list(FEATURE_NAMES),
        "metrics": trained["metrics"],
        "training_period_from": training_period_from.isoformat(),
        "training_period_to": training_period_to.isoformat(),
        "git_sha": git_sha,
        "lightgbm_version": __import__("lightgbm").__version__,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    passes = _passes_promotion(trained["metrics"])
    is_active = promote and passes

    async def _upsert() -> None:
        from trading.common.db import acquire, close_pool

        async with acquire() as conn:
            if is_active:
                await conn.execute(
                    "UPDATE research.models SET is_active = FALSE WHERE name = $1",
                    name,
                )
            await conn.execute(
                """
                INSERT INTO research.models
                    (id, name, version, path, metrics, params,
                     training_period_from, training_period_to,
                     git_sha, trained_at, is_active)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb,
                        $7, $8, $9, now(), $10)
                """,
                uuid.uuid4(),
                name,
                version,
                str(out_dir),
                json.dumps(trained["metrics"]),
                json.dumps(trained["metrics"].get("best_params", {})),
                training_period_from,
                training_period_to,
                git_sha,
                is_active,
            )
        await close_pool()

    asyncio.run(_upsert())
    return {
        "version": version,
        "path": str(out_dir),
        "passes_gate": passes,
        "is_active": is_active,
        "metrics": trained["metrics"],
    }


# --------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(prog="trading.cli.train_bb_ofi")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument(
        "--polybot-btc5m",
        default="/btc-tendencia-data/polybot-agent.db",
        help="BTC-Tendencia SQLite (1 Hz spot ticks). Slug encodes open_ts.",
    )
    ap.add_argument(
        "--polybot-agent",
        default="/btc-tendencia-data/polybot-agent.db",
        help="Second SQLite source. Defaults to same as --polybot-btc5m.",
    )
    ap.add_argument(
        "--slug-encodes-open-ts",
        action="store_true",
        default=True,
        help="(default true for BTC-Tendencia format) trailing slug = open_ts. "
        "Disable with --slug-encodes-close-ts for legacy polybot format.",
    )
    ap.add_argument(
        "--slug-encodes-close-ts",
        dest="slug_encodes_open_ts",
        action="store_false",
    )
    ap.add_argument("--optuna-trials", type=int, default=80)
    ap.add_argument("--time-budget-s", type=int, default=1800)
    ap.add_argument(
        "--microstructure-window-s",
        type=int,
        default=90,
        help="Trade aggregation window for the 5 microstructure features.",
    )
    ap.add_argument("--large-trade-threshold-usd", type=float, default=100_000.0)
    ap.add_argument(
        "--use-real-implied-prob",
        action="store_true",
        default=True,
        help="(default true) Read implied_prob_yes from polymarket_prices_history. "
        "Disable with --no-real-implied-prob.",
    )
    ap.add_argument(
        "--no-real-implied-prob",
        dest="use_real_implied_prob",
        action="store_false",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument(
        "--model-name",
        default="bb_residual_ofi_v1",
        help="Name written to research.models. Defaults to the strategy name.",
    )
    ap.add_argument(
        "--spots-source",
        choices=["polybot", "crypto_trades", "auto"],
        default="crypto_trades",
        help="Where to get the 1 Hz spot stream. crypto_trades scales to "
        "~35 d (default); polybot to ~8 d only.",
    )
    ap.add_argument(
        "--markets-source",
        choices=["sqlite", "postgres"],
        default="postgres",
        help="Where to source resolved markets. postgres uses "
        "polymarket_markets for the full universe; sqlite uses polybot's "
        "trade-resolved subset (~547 markets).",
    )
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)
    pg = _pg_dsn()
    log.info(
        "train_bb_ofi — period=%s..%s seed=%d model=%s",
        args.date_from,
        args.date_to,
        args.seed,
        args.model_name,
    )

    from trading.engine.data_loader import warn_if_polybot_stale

    for src in (args.polybot_btc5m, args.polybot_agent):
        if Path(src).exists():
            warn_if_polybot_stale(src, expected_window_end_ts=t_to.timestamp())

    # Snapshot SQLite to a writable tmpfs so concurrent writes by the
    # ingestor on the host don't trigger transient "database disk image
    # is malformed" — same trick as train_last90s.
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="tea_train_bb_ofi_"))

    def _snapshot(src: str) -> Path | None:
        p = Path(src)
        if not p.exists():
            return None
        dst = tmp_dir / p.name
        shutil.copy2(p, dst)
        return dst

    sqlite_sources: list[Path] = []
    for src in (args.polybot_btc5m, args.polybot_agent):
        snap = _snapshot(src)
        if snap is not None and snap.exists():
            sqlite_sources.append(snap)
    log.info("snapshotted SQLite sources: %s", sqlite_sources)
    if not sqlite_sources:
        log.error("no SQLite sources found; aborting")
        return 2

    # Resolved markets — labels via Binance 1m closes (audit fix).
    markets: list[dict] = []
    if args.markets_source == "postgres":
        markets = _load_settled_markets_from_pg(pg, t_from, t_to)
    else:
        for src in sqlite_sources:
            markets.extend(
                _load_resolved_markets(
                    src,
                    slug_encodes_open_ts=args.slug_encodes_open_ts,
                    pg_dsn=pg,
                )
            )
    # De-dup by slug, restrict to range.
    seen: set[str] = set()
    markets_in_range: list[dict] = []
    for m in markets:
        if m["slug"] in seen:
            continue
        if not (t_from.timestamp() <= m["close_ts"] <= t_to.timestamp()):
            continue
        seen.add(m["slug"])
        markets_in_range.append(m)
    log.info("resolved markets in range: %d", len(markets_in_range))
    if not markets_in_range:
        log.error("no markets in range; aborting")
        return 2

    samples = build_samples(
        markets_in_range,
        sqlite_sources=sqlite_sources,
        pg_dsn=pg,
        use_real_implied_prob=args.use_real_implied_prob,
        microstructure_window_s=args.microstructure_window_s,
        large_threshold_usd=args.large_trade_threshold_usd,
        spots_source=args.spots_source,
    )
    if len(samples) < 50:
        log.error(
            "too few samples (%d) — need ≥ 50 for a meaningful test split",
            len(samples),
        )
        return 2

    log.info(
        "training — n=%d optuna_trials=%d budget_s=%d",
        len(samples),
        args.optuna_trials,
        args.time_budget_s,
    )
    trained = train(
        samples,
        optuna_trials=args.optuna_trials,
        time_budget_s=args.time_budget_s,
        random_state=args.seed,
    )
    log.info("metrics: %s", trained["metrics"])

    result = write_artefacts(
        name=args.model_name,
        trained=trained,
        training_period_from=t_from,
        training_period_to=t_to,
        promote=args.promote,
    )
    log.info(
        "artefacts written — version=%s path=%s passes_gate=%s is_active=%s",
        result["version"],
        result["path"],
        result["passes_gate"],
        result["is_active"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
