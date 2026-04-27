from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import orjson
import websockets
from aiolimiter import AsyncLimiter
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from trading.common.config import get_settings
from trading.common.db import acquire, upsert_many
from trading.common.logging import get_logger
from trading.common.metrics import REGISTRY
from trading.ingest.base import (
    HealthStatus,
    IngestRateLimitError,
    IngestSourceDown,
    PolymarketIngestAdapter,
)
from trading.ingest.polymarket.slug import SLUG_PREFIX, window_for

log = get_logger(__name__)

CLOB_REST = "https://clob.polymarket.com"

# Gamma series_ids for the asset×5m families. Hardcoded — these are stable
# identifiers and are the only discovery key that lets us enumerate historical
# 5m markets (individual `?slug=` lookups drop archived markets after minutes).
BTC_UPDOWN_5M_SERIES_ID = 10684
ETH_UPDOWN_5M_SERIES_ID = 10683
GAMMA_EVENTS_PAGE_SIZE = 500

# Supported slug-pattern prefixes for `discover_markets`.
#
# 5m families have numeric `series_id`s (dense enumeration: every event in the
# series is a market of that family).
#
# 15m families have `seriesSlug='<asset>-up-or-down-15m'` only — Gamma honors
# no server-side filter for that, so /events must be paginated in default
# ordering and slug-prefix-filtered client-side. ~13 hits per 500 events.
# Pre-2026-04-28 there is NO 15m coverage in TEA — consumers must check
# `research.market_manifest_polymarket` before assuming N days of history.
SLUG_PREFIX_5M = "btc-updown-5m-"
SLUG_PREFIX_15M = "btc-updown-15m-"
SLUG_PREFIX_BTC_5M = SLUG_PREFIX_5M  # alias kept for backward compat
SLUG_PREFIX_BTC_15M = SLUG_PREFIX_15M  # alias
SLUG_PREFIX_ETH_5M = "eth-updown-5m-"
SLUG_PREFIX_ETH_15M = "eth-updown-15m-"

# Map slug prefix → numeric series_id for the dense (5m) path.
# 15m families are absent here on purpose — they take the global-pagination path.
SERIES_ID_BY_PREFIX: dict[str, int] = {
    SLUG_PREFIX_BTC_5M: BTC_UPDOWN_5M_SERIES_ID,
    SLUG_PREFIX_ETH_5M: ETH_UPDOWN_5M_SERIES_ID,
}

SUPPORTED_SLUG_PATTERNS = (
    SLUG_PREFIX_BTC_5M,
    SLUG_PREFIX_BTC_15M,
    SLUG_PREFIX_ETH_5M,
    SLUG_PREFIX_ETH_15M,
)
GLOBAL_EVENTS_SAFETY_PAGE_CAP = 60  # ≈ 30k events scanned worst-case


class PolymarketAdapter(PolymarketIngestAdapter):
    name = "polymarket"

    def __init__(self) -> None:
        s = get_settings()
        self.gamma_api = s.polymarket_gamma_api
        self.data_api = s.polymarket_data_api
        self.clob_ws = s.polymarket_clob_ws
        self.rate_limiter = AsyncLimiter(max_rate=15, time_period=1.0)
        self._gamma = httpx.AsyncClient(base_url=self.gamma_api, timeout=15.0)
        self._clob = httpx.AsyncClient(base_url=CLOB_REST, timeout=20.0)
        self._data = httpx.AsyncClient(base_url=self.data_api, timeout=15.0)
        self._last_msg_ts: datetime | None = None
        self._last_error: str | None = None
        self._msg_counter = REGISTRY.counter(
            "tea_ingest_messages_total", "messages received", {"adapter": self.name}
        )
        self._err_counter = REGISTRY.counter(
            "tea_ingest_errors_total", "errors raised", {"adapter": self.name}
        )
        self._age_gauge = REGISTRY.gauge(
            "tea_ingest_last_message_age_seconds",
            "seconds since last stream message",
            {"adapter": self.name},
        )

    async def aclose(self) -> None:
        await asyncio.gather(self._gamma.aclose(), self._clob.aclose(), self._data.aclose())

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _fetch_market_by_slug(self, slug: str) -> dict | None:
        async with self.rate_limiter:
            try:
                r = await self._gamma.get("/markets", params={"slug": slug})
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("gamma 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"gamma 5xx: {r.status_code}")
            r.raise_for_status()
            data = r.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict) and "data" in data:
            arr = data["data"]
            return arr[0] if arr else None
        return None

    async def discover_markets(self, slug_pattern: str, since: datetime) -> int:
        """Enumerate markets matching `slug_pattern` since `since`, upsert metadata.

        Dispatches by pattern:
          - 5m: queries Gamma /events filtered by `series_id=BTC_UPDOWN_5M_SERIES_ID`
                — efficient, every event in the series is a 5m market.
          - 15m: queries /events globally (no working server-side filter for 15m series),
                 paginates default ordering, filters slugs client-side, bounded by
                 GLOBAL_EVENTS_SAFETY_PAGE_CAP. Historical coverage capped at whatever
                 endDate is reached before the cap fires (typically ~6-7 days).

        Both paths upsert the same columns on `market_data.polymarket_markets` with
        ON CONFLICT DO NOTHING — safe to re-run.

        Returns the number of upsert-attempt rows.
        """
        if slug_pattern not in SUPPORTED_SLUG_PATTERNS:
            raise ValueError(f"unsupported slug_pattern: {slug_pattern!r}")
        # 5m families route via series_id (dense, fast). 15m families have no
        # numeric series_id and must paginate global /events.
        if slug_pattern in SERIES_ID_BY_PREFIX:
            return await self._discover_via_series(slug_pattern, since)
        return await self._discover_via_global_events(slug_pattern, since)

    async def _discover_via_series(self, slug_pattern: str, since: datetime) -> int:
        """5m path — paginate /events?series_id=<X> (dense, fast).

        Looks up the numeric series_id from SERIES_ID_BY_PREFIX. Each 5m
        family (BTC, ETH, future) has its own series_id.
        """
        series_id = SERIES_ID_BY_PREFIX.get(slug_pattern)
        if series_id is None:
            raise ValueError(f"no series_id mapped for slug_pattern={slug_pattern!r}")
        total = 0
        offset = 0
        since_epoch = int(since.astimezone(UTC).timestamp())
        reached_since = False
        while not reached_since:
            events = await self._fetch_events_page(offset, series_id=series_id)
            if not events:
                break
            rows: list[tuple] = []
            for ev in events:
                end_iso = ev.get("endDate")
                end_ts = self._iso_to_epoch(end_iso)
                if end_ts is not None and end_ts < since_epoch:
                    reached_since = True
                for m in ev.get("markets") or []:
                    row = self._market_row_from_event(m, ev, slug_pattern)
                    if row is not None:
                        rows.append(row)
            if rows:
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
                    rows,
                    ["condition_id"],
                )
                total += len(rows)
            offset += GAMMA_EVENTS_PAGE_SIZE
            log.info(
                "polymarket.discover.page",
                pattern=slug_pattern,
                offset=offset,
                page_markets=len(rows),
                total_so_far=total,
            )
        log.info("polymarket.discover.done", pattern=slug_pattern, upserted=total, since=since.isoformat())
        return total

    async def _discover_via_global_events(self, slug_pattern: str, since: datetime) -> int:
        """15m path — paginate /events globally, filter slugs client-side.

        Gamma offers no working server-side filter for the 15m series, so we
        paginate in default ordering (endDate desc) and check each event's
        markets array. Bounded by GLOBAL_EVENTS_SAFETY_PAGE_CAP to prevent
        runaway runtime; pre-2026-04-28 markets are unreachable through this
        path without raising the cap (rate-limit prohibitive).
        """
        total = 0
        since_epoch = int(since.astimezone(UTC).timestamp())
        for page in range(GLOBAL_EVENTS_SAFETY_PAGE_CAP):
            offset = page * GAMMA_EVENTS_PAGE_SIZE
            events = await self._fetch_events_page_global(offset)
            if not events:
                break
            rows: list[tuple] = []
            page_min_end_ts: int | None = None
            for ev in events:
                end_ts = self._iso_to_epoch(ev.get("endDate"))
                if end_ts is not None and (page_min_end_ts is None or end_ts < page_min_end_ts):
                    page_min_end_ts = end_ts
                for m in ev.get("markets") or []:
                    row = self._market_row_from_event(m, ev, slug_pattern)
                    if row is not None:
                        rows.append(row)
            if rows:
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
                    rows,
                    ["condition_id"],
                )
                total += len(rows)
            log.info(
                "polymarket.discover.page",
                pattern=slug_pattern,
                offset=offset,
                page_markets=len(rows),
                total_so_far=total,
                page_min_end=page_min_end_ts,
            )
            if page_min_end_ts is not None and page_min_end_ts < since_epoch:
                log.info("polymarket.discover.reached_since", pattern=slug_pattern, page=page)
                break
            if len(events) < GAMMA_EVENTS_PAGE_SIZE:
                break
        log.info(
            "polymarket.discover.done",
            pattern=slug_pattern,
            upserted=total,
            since=since.isoformat(),
            note="global-events scan bounded by GLOBAL_EVENTS_SAFETY_PAGE_CAP",
        )
        return total

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _fetch_events_page(
        self, offset: int, series_id: int = BTC_UPDOWN_5M_SERIES_ID
    ) -> list[dict]:
        """Generalized: caller passes the series_id. Default = BTC-5m for
        backward compat with any caller that still passes a single offset.
        """
        async with self.rate_limiter:
            try:
                r = await self._gamma.get(
                    "/events",
                    params={
                        "series_id": series_id,
                        "order": "endDate",
                        "ascending": "false",
                        "limit": GAMMA_EVENTS_PAGE_SIZE,
                        "offset": offset,
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
        if r.status_code == 429:
            raise IngestRateLimitError("gamma events 429")
        if r.status_code >= 500:
            raise IngestSourceDown(f"gamma events 5xx: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _fetch_events_page_global(self, offset: int) -> list[dict]:
        """Same as `_fetch_events_page` but without `series_id` filter — for 15m
        which lacks a numeric series and must be slug-prefix-filtered client-side.
        """
        async with self.rate_limiter:
            try:
                r = await self._gamma.get(
                    "/events",
                    params={
                        "order": "endDate",
                        "ascending": "false",
                        "limit": GAMMA_EVENTS_PAGE_SIZE,
                        "offset": offset,
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
        if r.status_code == 429:
            raise IngestRateLimitError("gamma events 429")
        if r.status_code >= 500:
            raise IngestSourceDown(f"gamma events 5xx: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _iso_to_epoch(s: str | None) -> int | None:
        if not s:
            return None
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None

    def _market_row_from_event(
        self, m: dict, ev: dict, slug_pattern: str = SLUG_PREFIX
    ) -> tuple | None:
        slug = m.get("slug") or ev.get("slug")
        if not slug or not slug.startswith(slug_pattern):
            return None
        condition_id = m.get("conditionId")
        if not condition_id:
            return None
        # Slug has the close_ts as suffix, definitive source.
        try:
            close_ts_from_slug = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            # Fall back to bucketing event endDate into a 5-min window.
            end_dt = (
                datetime.fromisoformat(str(ev.get("endDate", "")).replace("Z", "+00:00"))
                if ev.get("endDate")
                else datetime.now(tz=UTC)
            )
            close_ts_from_slug = window_for(end_dt).close_ts
        resolved = bool(m.get("closed") or m.get("resolved") or ev.get("closed"))
        outcome = None
        if resolved:
            # outcomePrices is a JSON string: ["0.0", "1.0"] or ["1.0","0.0"].
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
                for name, p in zip(outcomes, op, strict=False):
                    try:
                        if float(p) >= 0.99:
                            outcome = name
                            break
                    except Exception:
                        continue
        # Merge event-level fields (e.g., series, tags) into metadata for future use.
        merged = dict(m)
        merged["_event"] = {k: ev.get(k) for k in ("id", "slug", "ticker", "startDate", "endDate")}
        return (
            condition_id,
            slug,
            m.get("question") or ev.get("title", ""),
            close_ts_from_slug,
            resolved,
            outcome,
            self._to_dt(m.get("startDate") or ev.get("startDate")),
            self._to_dt(m.get("endDate") or ev.get("endDate")),
            self._to_dt(m.get("resolvedAt") or m.get("resolveTime")),
            orjson.dumps(merged).decode(),
        )

    @staticmethod
    def _to_dt(val) -> datetime | None:
        if not val:
            return None
        if isinstance(val, int | float):
            return datetime.fromtimestamp(val, tz=UTC)
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _market_row(m: dict, window_ts_fallback: int) -> tuple:
        def _to_ts(val) -> datetime | None:
            if not val:
                return None
            if isinstance(val, int | float):
                return datetime.fromtimestamp(val, tz=UTC)
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except Exception:
                return None

        resolved = bool(m.get("closed") or m.get("resolved") or False)
        outcome = None
        if resolved:
            outcome = m.get("outcome") or m.get("winningOutcome")
        return (
            m["conditionId"],
            m["slug"],
            m.get("question", ""),
            int(window_ts_fallback),
            resolved,
            outcome,
            _to_ts(m.get("startDate") or m.get("createdAt")),
            _to_ts(m.get("endDate") or m.get("closeTime")),
            _to_ts(m.get("resolvedAt") or m.get("resolveTime")),
            orjson.dumps(m).decode(),
        )

    async def backfill_market_prices(self, condition_id: str) -> int:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM market_data.polymarket_markets WHERE condition_id=$1",
                condition_id,
            )
        if not row or not row["metadata"]:
            return 0
        meta = json.loads(row["metadata"])
        tokens = self._extract_tokens(meta)
        if not tokens:
            return 0
        total = 0
        for token_id in tokens:
            rows = await self._fetch_price_history(token_id)
            if not rows:
                continue
            db_rows = [
                (
                    condition_id,
                    token_id,
                    datetime.fromtimestamp(int(p["t"]), tz=UTC),
                    Decimal(str(p["p"])),
                )
                for p in rows
            ]
            n = await upsert_many(
                "market_data.polymarket_prices",
                ["condition_id", "token_id", "ts", "price"],
                db_rows,
                ["condition_id", "token_id", "ts"],
            )
            total += n
        return total

    @staticmethod
    def _extract_tokens(meta: dict) -> list[str]:
        out: list[str] = []
        # Gamma can return tokens under different keys across API versions.
        for key in ("tokens", "clobTokenIds"):
            tokens = meta.get(key)
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    continue
            if isinstance(tokens, list):
                for t in tokens:
                    tid = t.get("token_id") if isinstance(t, dict) else t
                    if tid:
                        out.append(str(tid))
                if out:
                    return out
        return out

    async def _fetch_price_history(self, token_id: str) -> list[dict]:
        async with self.rate_limiter:
            try:
                r = await self._clob.get(
                    "/prices-history",
                    params={"market": token_id, "fidelity": 60},
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("clob 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"clob 5xx: {r.status_code}")
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("history", []) or []

    async def backfill_market_prices_history(self, condition_id: str) -> int:
        """Fetch historical prices for `condition_id` via CLOB /prices-history with
        explicit startTs/endTs and persist to `market_data.polymarket_prices_history`.

        Distinct from `backfill_market_prices` which writes to
        `polymarket_prices` (the live mid table) and passes only `fidelity` —
        the latter is rejected with HTTP 400 by the CLOB for archived markets.
        This method mirrors the canonical pattern in
        `scripts/backfill_polymarket_prices_history.py`: window =
        [open_time - 5min, close_time + 5min], fidelity=1.

        Returns total upserted rows (sum across YES + NO tokens).
        """
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT metadata->>'clobTokenIds' AS token_ids_json,
                       EXTRACT(EPOCH FROM open_time)::bigint AS open_unix,
                       EXTRACT(EPOCH FROM close_time)::bigint AS close_unix
                FROM market_data.polymarket_markets WHERE condition_id=$1
                """,
                condition_id,
            )
        if not row or not row["token_ids_json"] or row["open_unix"] is None or row["close_unix"] is None:
            return 0
        try:
            token_ids = json.loads(row["token_ids_json"])
        except Exception:
            return 0
        if not isinstance(token_ids, list) or len(token_ids) < 2:
            return 0
        start_ts = int(row["open_unix"]) - 300
        end_ts = int(row["close_unix"]) + 300
        total = 0
        for outcome_label, token_id in (("YES", str(token_ids[0])), ("NO", str(token_ids[1]))):
            history = await self._fetch_price_history_range(token_id, start_ts, end_ts)
            if not history:
                continue
            db_rows = [
                (
                    condition_id,
                    token_id,
                    outcome_label,
                    datetime.fromtimestamp(int(p["t"]), tz=UTC),
                    Decimal(str(p["p"])),
                )
                for p in history
                if "t" in p and "p" in p
            ]
            if db_rows:
                async with acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO market_data.polymarket_prices_history
                            (condition_id, token_id, outcome, ts, price)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (token_id, ts) DO NOTHING
                        """,
                        db_rows,
                    )
                total += len(db_rows)
        return total

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _fetch_price_history_range(
        self, token_id: str, start_ts: int, end_ts: int, fidelity: int = 1
    ) -> list[dict]:
        async with self.rate_limiter:
            try:
                r = await self._clob.get(
                    "/prices-history",
                    params={
                        "market": token_id,
                        "startTs": start_ts,
                        "endTs": end_ts,
                        "fidelity": fidelity,
                    },
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("clob 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"clob 5xx: {r.status_code}")
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("history", []) or []

    async def backfill_market_trades(self, condition_id: str, page_size: int = 500) -> int:
        """Fetch all historical trades for `condition_id` via Data API /trades.

        The Data API endpoint returns trades ordered most-recent first, paginated
        by `offset`. PK dedupe is `transactionHash`. ON CONFLICT DO NOTHING keeps
        re-runs idempotent. The CLOB /trades endpoint is auth-gated (401);
        Data API is the public alternative documented in Design.md I.7 §
        "Polymarket Data API (reconciliación / trades históricos)".

        Returns total upsert-attempt rows.
        """
        total = 0
        offset = 0
        while True:
            trades = await self._fetch_trades_page(condition_id, offset, page_size)
            if not trades:
                break
            db_rows: list[tuple] = []
            for t in trades:
                tx_hash = t.get("transactionHash")
                token_id = t.get("asset")
                ts_unix = t.get("timestamp")
                price = t.get("price")
                size = t.get("size")
                side = t.get("side")
                if not (tx_hash and token_id and ts_unix is not None and price is not None and size is not None and side):
                    continue
                db_rows.append(
                    (
                        condition_id,
                        str(token_id),
                        datetime.fromtimestamp(int(ts_unix), tz=UTC),
                        str(tx_hash),
                        Decimal(str(price)),
                        Decimal(str(size)),
                        str(side),
                    )
                )
            if db_rows:
                await upsert_many(
                    "market_data.polymarket_trades",
                    [
                        "condition_id",
                        "token_id",
                        "ts",
                        "tx_hash",
                        "price",
                        "size",
                        "side",
                    ],
                    db_rows,
                    ["condition_id", "tx_hash", "ts"],
                )
                total += len(db_rows)
            if len(trades) < page_size:
                break
            offset += page_size
        return total

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((IngestRateLimitError, IngestSourceDown)),
    )
    async def _fetch_trades_page(
        self, condition_id: str, offset: int, limit: int
    ) -> list[dict]:
        async with self.rate_limiter:
            try:
                r = await self._data.get(
                    "/trades",
                    params={"market": condition_id, "limit": limit, "offset": offset},
                )
            except httpx.HTTPError as e:
                raise IngestSourceDown(str(e)) from e
            if r.status_code == 429:
                raise IngestRateLimitError("data-api trades 429")
            if r.status_code >= 500:
                raise IngestSourceDown(f"data-api trades 5xx: {r.status_code}")
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []

    async def stream_prices(self, condition_ids: list[str]) -> None:
        # Map condition -> YES/NO token_ids from metadata.
        token_to_condition: dict[str, str] = {}
        assets_ids: list[str] = []
        async with acquire() as conn:
            rows = await conn.fetch(
                "SELECT condition_id, metadata FROM market_data.polymarket_markets "
                "WHERE condition_id = ANY($1::text[])",
                condition_ids,
            )
        for r in rows:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
            for tid in self._extract_tokens(meta):
                token_to_condition[tid] = r["condition_id"]
                assets_ids.append(tid)
        if not assets_ids:
            log.warning("polymarket.stream.empty", reason="no tokens resolved")
            return
        backoff = 1.0
        while True:
            try:
                # max_size generous: subscribe frame alone is ~1.7MB at 500+ markets;
                # book snapshots can also be large.
                async with websockets.connect(
                    self.clob_ws,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    log.info("polymarket.ws.connected", tokens=len(assets_ids))
                    backoff = 1.0
                    await ws.send(json.dumps({"type": "market", "assets_ids": assets_ids}))
                    async for raw in ws:
                        self._last_msg_ts = datetime.now(tz=UTC)
                        self._age_gauge.set(0.0)
                        msg = json.loads(raw)
                        await self._handle_clob_message(msg, token_to_condition)
            except Exception as e:
                self._last_error = str(e)
                self._err_counter.inc()
                log.warning("polymarket.ws.disconnect", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_clob_message(
        self, msg: dict | list, token_to_condition: dict[str, str]
    ) -> None:
        # CLOB sends either a single event or a list of events.
        events = msg if isinstance(msg, list) else [msg]
        rows: list[tuple] = []
        for ev in events:
            et = ev.get("event_type") or ev.get("type")
            tid = ev.get("asset_id") or ev.get("market")
            if not tid:
                continue
            cond = token_to_condition.get(str(tid))
            if not cond:
                continue
            ts = datetime.now(tz=UTC)
            price: Decimal | None = None
            if et == "book":
                price = self._mid_from_book(ev)
            elif et == "price_change":
                changes = ev.get("changes") or []
                if changes:
                    price = Decimal(str(changes[-1].get("price")))
            if price is None:
                continue
            rows.append((cond, str(tid), ts, price))
        if rows:
            await upsert_many(
                "market_data.polymarket_prices",
                ["condition_id", "token_id", "ts", "price"],
                rows,
                ["condition_id", "token_id", "ts"],
            )
            self._msg_counter.inc(len(rows))

    @staticmethod
    def _mid_from_book(ev: dict) -> Decimal | None:
        bids = ev.get("bids") or []
        asks = ev.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = Decimal(str(bids[0]["price"]))
            best_ask = Decimal(str(asks[0]["price"]))
            return (best_bid + best_ask) / Decimal(2)
        except Exception:
            return None

    def health(self) -> HealthStatus:
        if self._last_msg_ts is None:
            return HealthStatus(False, None, self._last_error, 0.0)
        age = (datetime.now(tz=UTC) - self._last_msg_ts).total_seconds()
        self._age_gauge.set(age)
        return HealthStatus(
            alive=age < 60,
            last_message_ts=self._last_msg_ts,
            last_error=self._last_error,
            messages_per_min=0.0,
        )
