"""Step -1.b: backfill 599 btc-updown-15m markets from the Step -1.a manifest.

Reads /tmp/step_minus_1a_result.json (produced by Step -1.a probe), upserts:
  - market_data.polymarket_markets       (metadata)
  - market_data.polymarket_prices        (per-token /prices-history)
  - market_data.polymarket_trades        (Data API /trades)
  - research.market_manifest_btc15m      (per-market coverage flags + counts)

All upserts use ON CONFLICT DO NOTHING semantics; safe to re-run.

This script does NOT extend the live-tap. The adapter has been extended to
support slug_pattern='btc-updown-15m-' for `discover_markets`, but enabling
the periodic refresh against 15m happens in the ingestor cli (separate
follow-up).

Usage:
    docker exec tea-ingestor python /app/scripts/backfill_btc15m_initial.py \\
        --manifest /tmp/step_minus_1a_result.json \\
        [--limit 50]   # optional: cap to first N markets for dry-run

Note: pre-2026-04-28 there is NO 15m coverage in TEA. Consumers that assume
30d+ history for `btc-updown-15m-` markets must check
`research.market_manifest_btc15m.has_book_data` and
`has_trade_data` flags before reasoning over the data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import orjson

from trading.common.db import acquire, upsert_many
from trading.ingest.polymarket.adapter import PolymarketAdapter

log = logging.getLogger("backfill.btc15m")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _to_dt(s: str | None) -> datetime | None:
    if not s or s == "None":
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


async def _upsert_market_metadata(market: dict) -> bool:
    """Upsert minimal market metadata — Step -1.a manifest doesn't carry the
    full Gamma object, so we synthesize a metadata blob with what we have plus
    a `_origin: 'step_minus_1a_manifest'` marker so a later live-tap pass can
    enrich it with full clobTokenIds when re-discovered.
    """
    cond = market["condition_id"]
    slug = market["slug"]
    open_dt = _to_dt(market.get("open_dt"))
    close_dt = _to_dt(market.get("close_dt"))
    close_ts = market.get("close_ts")
    resolved = bool(market.get("resolved"))
    outcome = market.get("outcome")

    # Need clobTokenIds to call /prices-history. The manifest does not include
    # them — fetch from Gamma /events lookup (single-event detail endpoint).
    return True  # actual upsert happens after we have token_ids


async def _enrich_with_tokens(adapter: PolymarketAdapter, condition_id: str, slug: str) -> dict | None:
    """Look up the live event by slug to fetch clobTokenIds.

    /markets?slug=... drops archived markets; we use /events?slug=... which
    keeps the event reachable for at least the recent past.
    """
    try:
        r = await adapter._gamma.get("/events", params={"slug": slug, "limit": 1})
        if r.status_code != 200:
            return None
        data = r.json()
        arr = data if isinstance(data, list) else data.get("data", [])
        if not arr:
            return None
        ev = arr[0]
        for m in ev.get("markets") or []:
            if m.get("conditionId") == condition_id or m.get("slug") == slug:
                return m
    except Exception:
        return None
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="/tmp/step_minus_1a_result.json")
    ap.add_argument("--limit", type=int, default=0, help="0 = no cap")
    ap.add_argument(
        "--prices",
        action="store_true",
        default=True,
        help="Backfill /prices-history per token (default on)",
    )
    ap.add_argument(
        "--trades",
        action="store_true",
        default=True,
        help="Backfill /trades per market (default on)",
    )
    ap.add_argument("--no-prices", dest="prices", action="store_false")
    ap.add_argument("--no-trades", dest="trades", action="store_false")
    args = ap.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)
    markets = manifest.get("markets") or []
    if args.limit:
        markets = markets[: args.limit]
    log.info("loaded manifest: %d markets to backfill", len(markets))

    adapter = PolymarketAdapter()

    # ---- Phase 1: enrich + upsert market metadata ----
    log.info("phase 1: upsert market metadata + clobTokenIds")
    enriched: list[dict] = []
    metadata_rows: list[tuple] = []
    for i, m in enumerate(markets):
        # Try Gamma /events?slug to get clobTokenIds
        full = await _enrich_with_tokens(adapter, m["condition_id"], m["slug"])
        token_ids: list[str] = []
        meta_blob: dict = dict(m)  # baseline from manifest
        if full:
            meta_blob.update(full)
            tk = full.get("clobTokenIds") or full.get("tokens")
            if isinstance(tk, str):
                try:
                    tk = json.loads(tk)
                except Exception:
                    tk = None
            if isinstance(tk, list):
                for t in tk:
                    tid = t.get("token_id") if isinstance(t, dict) else t
                    if tid:
                        token_ids.append(str(tid))
        meta_blob["_origin"] = "step_minus_1a_manifest"
        meta_blob["_token_ids"] = token_ids
        enriched.append({"market": m, "tokens": token_ids, "meta": meta_blob})

        open_dt = _to_dt(m.get("open_dt"))
        close_dt = _to_dt(m.get("close_dt"))
        metadata_rows.append(
            (
                m["condition_id"],
                m["slug"],
                "",  # question — manifest doesn't carry it; live-tap will enrich
                int(m.get("close_ts") or 0),
                bool(m.get("resolved")),
                m.get("outcome"),
                open_dt,
                close_dt,
                None,  # resolve_time
                orjson.dumps(meta_blob).decode(),
            )
        )

        if (i + 1) % 50 == 0 or (i + 1) == len(markets):
            log.info("phase 1 progress: %d/%d enriched", i + 1, len(markets))

    if metadata_rows:
        await upsert_many(
            "market_data.polymarket_markets",
            [
                "condition_id",
                "slug",
                "question",
                "window_ts",
                "resolved",
                "outcome",
                "open_time",
                "close_time",
                "resolve_time",
                "metadata",
            ],
            metadata_rows,
            ["condition_id"],
        )
    log.info("phase 1 done: %d markets upserted", len(metadata_rows))

    # ---- Phase 2: backfill /prices-history per token (with explicit time range) ----
    # Writes to market_data.polymarket_prices_history (the historical-backfill
    # table). This is distinct from polymarket_prices (live WS mid).
    prices_total = 0
    prices_missing_tokens = 0
    if args.prices:
        log.info("phase 2: /prices-history per token (historical, ranged)")
        for i, ent in enumerate(enriched):
            try:
                n = await adapter.backfill_market_prices_history(ent["market"]["condition_id"])
                prices_total += n
                if not ent["tokens"]:
                    prices_missing_tokens += 1
            except Exception as e:
                log.warning("prices_err cond=%s err=%s", ent["market"]["condition_id"], e)
            if (i + 1) % 25 == 0 or (i + 1) == len(enriched):
                log.info("phase 2 progress: %d/%d  prices_total=%d", i + 1, len(enriched), prices_total)
        log.info(
            "phase 2 done: %d price rows, %d markets had no token metadata",
            prices_total,
            prices_missing_tokens,
        )

    # ---- Phase 3: backfill /trades per market ----
    trades_total = 0
    trades_per_market: dict[str, int] = {}
    if args.trades:
        log.info("phase 3: /trades per market")
        for i, ent in enumerate(enriched):
            cond = ent["market"]["condition_id"]
            try:
                n = await adapter.backfill_market_trades(cond)
                trades_total += n
                trades_per_market[cond] = n
            except Exception as e:
                log.warning("trades_err cond=%s err=%s", cond, e)
            if (i + 1) % 25 == 0 or (i + 1) == len(enriched):
                log.info(
                    "phase 3 progress: %d/%d  trades_total=%d",
                    i + 1,
                    len(enriched),
                    trades_total,
                )
        log.info("phase 3 done: %d trades total", trades_total)

    # ---- Phase 4: persist research.market_manifest_btc15m ----
    log.info("phase 4: research.market_manifest_btc15m")
    async with acquire() as conn:
        rows: list[tuple] = []
        for ent in enriched:
            m = ent["market"]
            cond = m["condition_id"]
            # Per-market actual row counts
            n_prices = await conn.fetchval(
                "SELECT count(*) FROM market_data.polymarket_prices_history WHERE condition_id=$1",
                cond,
            )
            n_trades = await conn.fetchval(
                "SELECT count(*) FROM market_data.polymarket_trades WHERE condition_id=$1",
                cond,
            )
            rows.append(
                (
                    cond,
                    m["slug"],
                    _to_dt(m.get("open_dt")),
                    _to_dt(m.get("close_dt")),
                    m.get("outcome"),
                    bool(n_prices and n_prices > 0),
                    bool(n_trades and n_trades > 0),
                    int(n_prices or 0),
                    int(n_trades or 0),
                    datetime.now(tz=UTC),
                    datetime.now(tz=UTC),
                )
            )
        # Use raw SQL upsert because research.market_manifest_btc15m has a
        # unique-on-PK semantic that needs explicit conflict target.
        await conn.executemany(
            """
            INSERT INTO research.market_manifest_btc15m (
                condition_id, slug, open_ts, close_ts, resolution,
                has_book_data, has_trade_data, prices_n_rows, trades_n_rows,
                enumerated_at, last_validated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (condition_id) DO UPDATE SET
                slug              = EXCLUDED.slug,
                open_ts           = EXCLUDED.open_ts,
                close_ts          = EXCLUDED.close_ts,
                resolution        = EXCLUDED.resolution,
                has_book_data     = EXCLUDED.has_book_data,
                has_trade_data    = EXCLUDED.has_trade_data,
                prices_n_rows     = EXCLUDED.prices_n_rows,
                trades_n_rows     = EXCLUDED.trades_n_rows,
                last_validated_at = EXCLUDED.last_validated_at
            """,
            rows,
        )
    log.info("phase 4 done: %d manifest rows", len(rows))

    # ---- Final summary ----
    log.info("=" * 70)
    log.info("BACKFILL SUMMARY")
    log.info("=" * 70)
    log.info("Markets processed       : %d", len(enriched))
    log.info("Prices rows ingested    : %d", prices_total)
    log.info("Trades rows ingested    : %d", trades_total)
    log.info("Markets w/ no token meta: %d", prices_missing_tokens)
    log.info("Trades p50              : %.0f", _percentile(list(trades_per_market.values()), 50))
    log.info("Trades p90              : %.0f", _percentile(list(trades_per_market.values()), 90))
    await adapter.aclose()
    return 0


def _percentile(xs: list[int], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(p / 100 * (len(s) - 1))))
    return float(s[k])


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
