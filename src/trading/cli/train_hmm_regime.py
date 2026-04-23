"""Train the 4-state Gaussian HMM regime detector (ADR 0012).

Reads Binance 5 m BTCUSDT candles from ``market_data.crypto_ohlcv``,
fits ``hmmlearn.hmm.GaussianHMM`` with ``n_components=4``, re-labels
states via ``canonical_label_order``, and writes the pickle + meta
bundle to ``models/hmm_regime_btc5m/<version>/model.pkl`` plus a row
in ``research.models``. ``--promote`` flips ``is_active=TRUE`` for
the new row.

Usage::

    docker compose exec tea-engine python -m trading.cli.train_hmm_regime \\
        --from 2025-04-23 --to 2026-04-23 --promote
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("cli.train_hmm_regime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _fetch_closes(pg_dsn: str, since_ts: int, until_ts: int) -> list[float]:
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT close FROM market_data.crypto_ohlcv "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m' "
                "AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "ORDER BY ts ASC",
                (since_ts, until_ts),
            )
            return [float(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--promote", action="store_true")
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.date_from).replace(tzinfo=UTC)
    t_to = datetime.fromisoformat(args.date_to).replace(tzinfo=UTC)

    pg_dsn = (
        f"postgresql://{os.environ.get('TEA_PG_USER','tea')}:"
        f"{os.environ.get('TEA_PG_PASSWORD','')}@"
        f"{os.environ.get('TEA_PG_HOST','tea-postgres')}:"
        f"{os.environ.get('TEA_PG_PORT','5432')}/"
        f"{os.environ.get('TEA_PG_DB','trading_edge')}"
    )

    closes = _fetch_closes(pg_dsn, int(t_from.timestamp()), int(t_to.timestamp()))
    log.info("closes fetched: %d", len(closes))
    if len(closes) < 500:
        log.error("too few candles (%d) — need ≥ 500", len(closes))
        return 2

    from trading.engine.features.hmm_regime import (
        build_feature_matrix,
        canonical_label_order,
    )

    features = build_feature_matrix(closes)
    log.info("feature rows: %d", len(features))

    import numpy as np
    from hmmlearn import hmm as hmmlib

    X = np.asarray(features, dtype=np.float64)
    model = hmmlib.GaussianHMM(
        n_components=4,
        covariance_type="full",
        n_iter=200,
        tol=1e-4,
        random_state=42,
    )
    model.fit(X)
    log.info("fit converged: %s, score=%.4f", model.monitor_.converged, float(model.score(X)))

    means = [
        (float(model.means_[i, 0]), float(model.means_[i, 1]))
        for i in range(model.n_components)
    ]
    labels = canonical_label_order(means)
    log.info("state labels: %s", labels)

    version = f"hmm_{datetime.now(tz=UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}"
    out_dir = Path("models") / "hmm_regime_btc5m" / version
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "model.pkl", "wb") as f:
        pickle.dump({"model": model, "labels": labels}, f)
    meta = {
        "n_components": 4,
        "n_samples": len(features),
        "state_means": means,
        "state_labels": labels,
        "score": float(model.score(X)),
        "training_period_from": t_from.isoformat(),
        "training_period_to": t_to.isoformat(),
        "git_sha": _git_sha(),
        "hmmlearn_version": __import__("hmmlearn").__version__,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    log.info("saved %s/model.pkl", out_dir)

    import asyncio

    async def _upsert():
        from trading.common.db import acquire, close_pool

        async with acquire() as conn:
            if args.promote:
                await conn.execute(
                    "UPDATE research.models SET is_active = FALSE WHERE name = $1",
                    "hmm_regime_btc5m",
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
                uuid.uuid4(), "hmm_regime_btc5m", version, str(out_dir),
                json.dumps(meta),
                json.dumps({}),
                t_from, t_to,
                meta["git_sha"], bool(args.promote),
            )
        await close_pool()

    asyncio.run(_upsert())
    log.info("done: version=%s promoted=%s", version, bool(args.promote))
    return 0


if __name__ == "__main__":
    sys.exit(main())
