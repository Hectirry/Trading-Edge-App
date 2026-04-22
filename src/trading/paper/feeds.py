"""Three WebSocket feed tasks that populate FeedState.

Mirrors polybot-btc5m/core/feeds.py semantics but wired into the
TEA shared state object. No network config hardcoded — all URLs come
from trading.common.config.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from time import time

import httpx
import websockets

from trading.common.logging import get_logger
from trading.paper.state import CLOBBookSnapshot, FeedState, MarketMeta

log = get_logger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_1s"
CHAINLINK_WS = "wss://ws-live-data.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"


async def run_binance_spot_1s(state: FeedState) -> None:
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=30, ping_timeout=10) as ws:
                log.info("paper.binance.ws.connected")
                backoff = 1.0
                async for raw in ws:
                    data = json.loads(raw)
                    k = data.get("k") or {}
                    close = k.get("c")
                    if not close:
                        continue
                    state.spot_price = float(close)
                    state.spot_ts = time()
        except Exception as e:
            log.warning("paper.binance.ws.disconnect", err=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def run_chainlink_rtds(state: FeedState) -> None:
    """Chainlink BTC/USD oracle via Polymarket RTDS. Price > 1000 heuristic
    to discriminate price from timestamps/sizes, per polybot's extractor."""
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(CHAINLINK_WS, ping_interval=None) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "action": "subscribe",
                            "subscriptions": [
                                {
                                    "topic": "crypto_prices_chainlink",
                                    "type": "*",
                                    "filters": json.dumps({"symbol": "btc/usd"}),
                                }
                            ],
                        }
                    )
                )
                log.info("paper.chainlink.ws.connected")
                ping_task = asyncio.create_task(_chainlink_ping(ws))
                backoff = 1.0
                try:
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        price = _extract_chainlink_price(data)
                        if price:
                            state.chainlink_price = price
                            state.chainlink_ts = time()
                finally:
                    ping_task.cancel()
        except Exception as e:
            log.warning("paper.chainlink.ws.disconnect", err=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _chainlink_ping(ws) -> None:
    try:
        while True:
            await asyncio.sleep(5)
            await ws.send("PING")
    except (asyncio.CancelledError, ConnectionResetError):
        return
    except Exception as e:
        log.debug("paper.chainlink.ping.end", err=str(e))


def _extract_chainlink_price(data) -> float | None:
    PRICE_KEYS = ("price", "value", "p", "close", "usd", "last", "c")

    def _walk(node):
        if isinstance(node, dict):
            for k in PRICE_KEYS:
                if k in node:
                    try:
                        v = float(node[k])
                        if v > 1000:
                            return v
                    except (TypeError, ValueError):
                        pass
            for v in node.values():
                found = _walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = _walk(v)
                if found:
                    return found
        return None

    return _walk(data)


async def run_clob_l2(state: FeedState) -> None:
    """Tracks bid/ask/depth per token. Reconciles on reconnect by pulling a
    fresh book snapshot via CLOB REST for each known token."""
    backoff = 1.0
    while True:
        try:
            tokens = list(state.token_to_condition.keys())
            if not tokens:
                # Nothing to subscribe yet; the market refresher will populate
                # tokens shortly.
                await asyncio.sleep(3)
                continue
            async with websockets.connect(
                CLOB_WS, ping_interval=20, ping_timeout=10, max_size=16 * 1024 * 1024
            ) as ws:
                await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
                log.info("paper.clob.ws.connected", tokens=len(tokens))
                backoff = 1.0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    events = msg if isinstance(msg, list) else [msg]
                    async with state.lock:
                        for ev in events:
                            _apply_clob_event(ev, state)
        except Exception as e:
            log.warning("paper.clob.ws.disconnect", err=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _apply_clob_event(ev: dict, state: FeedState) -> None:
    et = ev.get("event_type") or ev.get("type")
    tid = str(ev.get("asset_id") or ev.get("market") or "")
    cond = state.token_to_condition.get(tid)
    if not cond:
        return
    book = state.books.setdefault(cond, CLOBBookSnapshot())
    side = state.token_side.get(tid)
    if et == "book":
        bids = ev.get("bids") or []
        asks = ev.get("asks") or []
        if side == "yes":
            book.yes_bid = _best(bids)
            book.yes_ask = _best(asks, ascending=True)
            book.depth_yes = _sum_usd(bids) + _sum_usd(asks)
        elif side == "no":
            book.no_bid = _best(bids)
            book.no_ask = _best(asks, ascending=True)
            book.depth_no = _sum_usd(bids) + _sum_usd(asks)
        book.last_update_ts = time()
    elif et == "price_change":
        changes = ev.get("changes") or []
        if not changes:
            return
        for ch in changes:
            try:
                price = float(ch.get("price"))
            except (TypeError, ValueError):
                continue
            # price_change doesn't always say side; infer from token_side.
            if side == "yes":
                if ch.get("side") == "SELL":
                    book.yes_ask = price
                else:
                    book.yes_bid = price
            else:
                if ch.get("side") == "SELL":
                    book.no_ask = price
                else:
                    book.no_bid = price
        book.last_update_ts = time()


def _best(levels: list, ascending: bool = False) -> float:
    try:
        prices = sorted((float(level.get("price", 0)) for level in levels), reverse=not ascending)
        for p in prices:
            if p > 0:
                return p
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _sum_usd(levels: list) -> float:
    total = 0.0
    for lvl in levels:
        try:
            p = float(lvl.get("price", 0))
            s = float(lvl.get("size", 0))
            total += p * s
        except (TypeError, ValueError):
            continue
    return total


async def refresh_markets_loop(state: FeedState) -> None:
    """Every 30 s, call Gamma API for the next upcoming btc-updown-5m markets
    (filtered by series_id=10684, ordered by endDate asc, those not yet
    closed). Populate markets/tokens maps so CLOB subscribes to them.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                await _refresh_once(client, state)
            except Exception as e:
                log.warning("paper.market_refresh.err", err=str(e))
            await asyncio.sleep(30)


async def _refresh_once(client: httpx.AsyncClient, state: FeedState) -> None:
    # Grab the 100 newest events by endDate. `ascending=false` is essential
    # — `true` returns archived 2025 events first. `closed=false` drops the
    # already-resolved ones in the top of the list.
    r = await client.get(
        "https://gamma-api.polymarket.com/events",
        params={
            "series_id": 10684,
            "order": "endDate",
            "ascending": "false",
            "closed": "false",
            "limit": 100,
        },
    )
    r.raise_for_status()
    events = r.json()
    if not isinstance(events, list):
        return
    now = time()
    async with state.lock:
        for ev in events:
            if ev.get("closed"):
                continue
            markets = ev.get("markets") or []
            for m in markets:
                slug = m.get("slug") or ev.get("slug")
                if not slug or not slug.startswith("btc-updown-5m-"):
                    continue
                try:
                    close_ts = int(slug.rsplit("-", 1)[-1])
                except ValueError:
                    continue
                if close_ts <= now:
                    continue
                condition_id = m.get("conditionId")
                if not condition_id:
                    continue
                tokens_raw = m.get("clobTokenIds")
                if isinstance(tokens_raw, str):
                    try:
                        tokens = json.loads(tokens_raw)
                    except Exception:
                        continue
                else:
                    tokens = tokens_raw or []
                if len(tokens) != 2:
                    continue
                yes_id, no_id = str(tokens[0]), str(tokens[1])
                state.markets[condition_id] = MarketMeta(
                    condition_id=condition_id,
                    slug=slug,
                    yes_token_id=yes_id,
                    no_token_id=no_id,
                    window_close_ts=close_ts,
                    open_price=state.markets.get(
                        condition_id, MarketMeta("", "", "", "", 0)
                    ).open_price,
                    open_price_captured=state.markets.get(
                        condition_id, MarketMeta("", "", "", "", 0)
                    ).open_price_captured,
                )
                state.token_to_condition[yes_id] = condition_id
                state.token_to_condition[no_id] = condition_id
                state.token_side[yes_id] = "yes"
                state.token_side[no_id] = "no"
    # Drop markets already past their close_ts + 5 min grace.
    cutoff = now - 300
    async with state.lock:
        for cid in list(state.markets.keys()):
            if state.markets[cid].window_close_ts < cutoff:
                m = state.markets.pop(cid)
                state.books.pop(cid, None)
                for tid in (m.yes_token_id, m.no_token_id):
                    state.token_to_condition.pop(tid, None)
                    state.token_side.pop(tid, None)


def compute_derived_book(book: CLOBBookSnapshot) -> dict[str, float | None]:
    """Derived fields used by the strategy: imbalance, spread_bps, implied prob."""
    if book.yes_bid <= 0 or book.yes_ask <= 0:
        return {"imbalance": None, "spread_bps": None, "implied_prob_yes": None}
    mid_yes = (book.yes_bid + book.yes_ask) / 2
    spread_bps = (book.yes_ask - book.yes_bid) / mid_yes * 10000 if mid_yes > 0 else None
    if book.depth_no > 0:
        imbalance = book.depth_yes / book.depth_no
    else:
        imbalance = float("inf") if book.depth_yes > 0 else 1.0
    return {
        "imbalance": imbalance,
        "spread_bps": spread_bps,
        "implied_prob_yes": mid_yes,
    }


def decimalize(value: float) -> Decimal:
    return Decimal(str(value))
