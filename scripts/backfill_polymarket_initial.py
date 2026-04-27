"""Generalized polymarket initial backfill — supports BTC/ETH × 5m/15m.

Replaces `backfill_btc15m_initial.py` (which used the legacy
research.market_manifest_btc15m table that's been dropped in favor of
research.market_manifest_polymarket).

Per family:
  1. Enumerate via Gamma — series_id for 5m, global pagination for 15m.
  2. Upsert metadata to market_data.polymarket_markets.
  3. (Unless --skip-prices) backfill /prices-history with explicit time range.
  4. (Unless --skip-trades) backfill Data API /trades.
  5. Persist coverage flags to research.market_manifest_polymarket
     with (asset_class, horizon) labels.

All upserts are idempotent (ON CONFLICT DO NOTHING / DO UPDATE).

Usage:
    docker exec tea-ingestor python /app/scripts/backfill_polymarket_initial.py \\
        --asset-class BTC --horizon 5m --lookback-days 7 --skip-metadata --skip-prices

    docker exec tea-ingestor python /app/scripts/backfill_polymarket_initial.py \\
        --asset-class ETH --horizon 5m --lookback-days 7

    docker exec tea-ingestor python /app/scripts/backfill_polymarket_initial.py \\
        --asset-class ETH --horizon 15m --lookback-days 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import orjson

from trading.common.db import acquire, upsert_many
from trading.ingest.polymarket.adapter import (
    GAMMA_EVENTS_PAGE_SIZE,
    SERIES_ID_BY_PREFIX,
    SLUG_PREFIX_BTC_5M,
    SLUG_PREFIX_BTC_15M,
    SLUG_PREFIX_ETH_5M,
    SLUG_PREFIX_ETH_15M,
    PolymarketAdapter,
)

log = logging.getLogger("backfill.polymarket")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


PREFIX_BY_ASSET_HORIZON = {
    ("BTC", "5m"): SLUG_PREFIX_BTC_5M,
    ("BTC", "15m"): SLUG_PREFIX_BTC_15M,
    ("ETH", "5m"): SLUG_PREFIX_ETH_5M,
    ("ETH", "15m"): SLUG_PREFIX_ETH_15M,
}


def _to_dt(s: str | None) -> datetime | None:
    if not s or s in ("None", "null"):
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


async def _enumerate(adapter: PolymarketAdapter, prefix: str, since: datetime) -> list[dict]:
    """Discover markets matching prefix since `since`. Returns list of dicts
    with {condition_id, slug, open_dt, close_dt, close_ts, resolved, outcome,
    metadata}. Uses series_id for 5m families, global pagination for 15m.
    """
    out: list[dict] = []
    seen: set[str] = set()
    since_epoch = int(since.timestamp())
    series_id = SERIES_ID_BY_PREFIX.get(prefix)
    use_series = series_id is not None

    offset = 0
    pages = 60 if not use_series else 30  # 15m needs more pagination
    for page in range(pages):
        if use_series:
            events = await adapter._fetch_events_page(offset=offset, series_id=series_id)
        else:
            events = await adapter._fetch_events_page_global(offset=offset)
        if not events:
            break
        page_min_end_ts: int | None = None
        for ev in events:
            end_dt = _to_dt(ev.get("endDate"))
            if end_dt is not None:
                end_ts_int = int(end_dt.timestamp())
                if page_min_end_ts is None or end_ts_int < page_min_end_ts:
                    page_min_end_ts = end_ts_int
            for m in ev.get("markets") or []:
                slug = m.get("slug") or ev.get("slug") or ""
                if not slug.startswith(prefix):
                    continue
                cond = m.get("conditionId")
                if not cond or cond in seen:
                    continue
                try:
                    close_ts = int(slug.rsplit("-", 1)[-1])
                except ValueError:
                    close_ts = None
                resolved = bool(m.get("closed") or m.get("resolved") or ev.get("closed"))
                outcome = None
                if resolved:
                    op = m.get("outcomePrices")
                    if isinstance(op, str):
                        try:
                            op = json.loads(op)
                        except Exception:
                            op = None
                    outcomes = m.get("outcomes")
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except Exception:
                            outcomes = None
                    if isinstance(op, list) and isinstance(outcomes, list) and len(op) == len(outcomes):
                        for nm, p in zip(outcomes, op, strict=False):
                            try:
                                if float(p) >= 0.99:
                                    outcome = nm
                                    break
                            except Exception:
                                pass
                seen.add(cond)
                merged = dict(m)
                merged["_event"] = {k: ev.get(k) for k in ("id", "slug", "ticker", "startDate", "endDate")}
                out.append(
                    {
                        "condition_id": cond,
                        "slug": slug,
                        "close_ts": close_ts,
                        "open_dt": _to_dt(m.get("startDate") or ev.get("startDate")),
                        "close_dt": _to_dt(m.get("endDate") or ev.get("endDate")),
                        "resolved": resolved,
                        "outcome": outcome,
                        "metadata": merged,
                    }
                )
        log.info(
            "enumerate_page prefix=%s page=%d n=%d page_min_end=%s",
            prefix, page, len(out), page_min_end_ts,
        )
        if page_min_end_ts is not None and page_min_end_ts < since_epoch:
            break
        if len(events) < GAMMA_EVENTS_PAGE_SIZE:
            break
        offset += GAMMA_EVENTS_PAGE_SIZE
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-class", required=True, choices=["BTC", "ETH"])
    ap.add_argument("--horizon", required=True, choices=["5m", "15m"])
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--skip-metadata", action="store_true",
                    help="Reuse existing polymarket_markets entries; only upsert manifest + backfill trades/prices.")
    ap.add_argument("--skip-prices", action="store_true",
                    help="Skip /prices-history (e.g. BTC-5m where it's already populated).")
    ap.add_argument("--skip-trades", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    prefix = PREFIX_BY_ASSET_HORIZON[(args.asset_class, args.horizon)]
    since = datetime.now(tz=UTC) - timedelta(days=args.lookback_days)
    log.info("start asset=%s horizon=%s prefix=%s since=%s",
             args.asset_class, args.horizon, prefix, since.isoformat())

    adapter = PolymarketAdapter()
    try:
        markets = await _enumerate(adapter, prefix, since)
    except Exception as e:
        log.error("enumerate failed: %s", e)
        await adapter.aclose()
        return 1
    if args.limit:
        markets = markets[: args.limit]
    log.info("enumerated %d markets", len(markets))

    # ─── Phase 1: upsert metadata to polymarket_markets ───────────────
    if not args.skip_metadata and markets:
        rows: list[tuple] = []
        for m in markets:
            rows.append(
                (
                    m["condition_id"],
                    m["slug"],
                    m["metadata"].get("question") or "",
                    int(m.get("close_ts") or 0),
                    bool(m["resolved"]),
                    m.get("outcome"),
                    m.get("open_dt"),
                    m.get("close_dt"),
                    None,  # resolve_time
                    orjson.dumps(m["metadata"]).decode(),
                )
            )
        await upsert_many(
            "market_data.polymarket_markets",
            [
                "condition_id", "slug", "question", "window_ts", "resolved",
                "outcome", "open_time", "close_time", "resolve_time", "metadata",
            ],
            rows,
            ["condition_id"],
        )
        log.info("phase1 upsert markets: %d", len(rows))
    else:
        log.info("phase1 SKIPPED (skip_metadata or no markets)")

    # ─── Phase 2: prices history ──────────────────────────────────────
    prices_total = 0
    if not args.skip_prices:
        for i, m in enumerate(markets):
            try:
                n = await adapter.backfill_market_prices_history(m["condition_id"])
                prices_total += n
            except Exception as e:
                log.warning("prices_err cond=%s err=%s", m["condition_id"], e)
            if (i + 1) % 50 == 0 or (i + 1) == len(markets):
                log.info("phase2 progress: %d/%d prices_total=%d", i + 1, len(markets), prices_total)
    else:
        log.info("phase2 SKIPPED (skip_prices)")

    # ─── Phase 3: trades ──────────────────────────────────────────────
    trades_total = 0
    if not args.skip_trades:
        for i, m in enumerate(markets):
            try:
                n = await adapter.backfill_market_trades(m["condition_id"])
                trades_total += n
            except Exception as e:
                log.warning("trades_err cond=%s err=%s", m["condition_id"], e)
            if (i + 1) % 50 == 0 or (i + 1) == len(markets):
                log.info("phase3 progress: %d/%d trades_total=%d", i + 1, len(markets), trades_total)
    else:
        log.info("phase3 SKIPPED")

    # ─── Phase 4: manifest ────────────────────────────────────────────
    async with acquire() as conn:
        rows: list[tuple] = []
        for m in markets:
            cond = m["condition_id"]
            n_p = await conn.fetchval(
                "SELECT count(*) FROM market_data.polymarket_prices_history WHERE condition_id=$1",
                cond,
            )
            n_t = await conn.fetchval(
                "SELECT count(*) FROM market_data.polymarket_trades WHERE condition_id=$1",
                cond,
            )
            rows.append(
                (
                    cond,
                    m["slug"],
                    args.asset_class,
                    args.horizon,
                    m.get("open_dt"),
                    m.get("close_dt"),
                    m.get("outcome"),
                    bool(n_p and n_p > 0),
                    bool(n_t and n_t > 0),
                    int(n_p or 0),
                    int(n_t or 0),
                    datetime.now(tz=UTC),
                    datetime.now(tz=UTC),
                )
            )
        await conn.executemany(
            """
            INSERT INTO research.market_manifest_polymarket (
                condition_id, slug, asset_class, horizon, open_ts, close_ts, resolution,
                has_book_data, has_trade_data, prices_n_rows, trades_n_rows,
                enumerated_at, last_validated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (condition_id) DO UPDATE SET
                asset_class       = EXCLUDED.asset_class,
                horizon           = EXCLUDED.horizon,
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
    log.info("phase4 manifest: %d rows for %s-%s", len(rows), args.asset_class, args.horizon)
    log.info("DONE asset=%s horizon=%s markets=%d prices=%d trades=%d",
             args.asset_class, args.horizon, len(markets), prices_total, trades_total)
    await adapter.aclose()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
