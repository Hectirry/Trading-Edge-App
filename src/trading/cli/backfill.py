from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from trading.common.logging import configure_logging, get_logger
from trading.ingest.binance import BinanceAdapter
from trading.ingest.bybit import BybitAdapter
from trading.ingest.coinbase import CoinbaseAdapter
from trading.ingest.kraken import KrakenAdapter
from trading.ingest.okx import OkxAdapter
from trading.ingest.polymarket import PolymarketAdapter
from trading.ingest.polymarket.slug import SLUG_PREFIX

log = get_logger("cli.backfill")


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


async def _run(args: argparse.Namespace) -> None:
    if args.broker == "binance":
        a = BinanceAdapter()
        try:
            n = await a.backfill_ohlcv(args.symbol, args.interval, args.from_ts, args.to_ts)
            log.info("backfill.done", broker="binance", rows=n)
        finally:
            await a.aclose()
    elif args.broker == "bybit":
        a = BybitAdapter()
        try:
            n = await a.backfill_ohlcv(args.symbol, args.interval, args.from_ts, args.to_ts)
            log.info("backfill.done", broker="bybit", rows=n)
        finally:
            await a.aclose()
    elif args.broker == "coinbase":
        a = CoinbaseAdapter()
        try:
            n = await a.backfill_ohlcv(args.symbol, args.interval, args.from_ts, args.to_ts)
            log.info("backfill.done", broker="coinbase", rows=n)
        finally:
            await a.aclose()
    elif args.broker == "okx":
        a = OkxAdapter()
        try:
            n = await a.backfill_ohlcv(args.symbol, args.interval, args.from_ts, args.to_ts)
            log.info("backfill.done", broker="okx", rows=n)
        finally:
            await a.aclose()
    elif args.broker == "kraken":
        a = KrakenAdapter()
        try:
            n = await a.backfill_ohlcv(args.symbol, args.interval, args.from_ts, args.to_ts)
            log.info("backfill.done", broker="kraken", rows=n)
        finally:
            await a.aclose()
    elif args.broker == "polymarket":
        a = PolymarketAdapter()
        try:
            n = await a.discover_markets(SLUG_PREFIX, args.from_ts)
            log.info("discover.done", broker="polymarket", rows=n)
        finally:
            await a.aclose()
    else:
        raise SystemExit(f"unknown broker: {args.broker}")


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="trading.cli.backfill")
    p.add_argument(
        "--broker",
        required=True,
        choices=["binance", "bybit", "coinbase", "okx", "kraken", "polymarket"],
    )
    p.add_argument("--symbol", default="")
    p.add_argument("--interval", default="")
    p.add_argument("--from", dest="from_ts", required=True, type=_parse_ts)
    p.add_argument("--to", dest="to_ts", default=None, type=_parse_ts)
    args = p.parse_args()
    if args.to_ts is None:
        args.to_ts = datetime.now(tz=UTC)
    if args.broker in ("binance", "bybit", "coinbase", "okx", "kraken"):
        if not args.symbol or not args.interval:
            raise SystemExit("--symbol and --interval required for crypto brokers")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
