"""One-off forensics for the 24-Apr trend_confirm_t1_v1 FAIL backtest.

Reads the 31 trades of backtest_id=21dcdc91-994d-453c-a374-866c1168f4a7,
recomputes settle independently against canonical Binance 1m OHLCV (the
same source backfill_paper_settles.py uses), and reports per-trade
agreement vs the stored resolution. Also prints the diagnostic checks
listed in the forensic mission.

Run: docker compose exec tea-engine python scripts/forensics_trend_fail.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from trading.common.db import acquire, close_pool

BACKTEST_ID = "21dcdc91-994d-453c-a374-866c1168f4a7"


@dataclass
class TradeRow:
    trade_idx: int
    slug: str
    strategy_side: str
    entry_ts: float
    entry_price: float
    exit_price: float
    pnl: float
    t_in_window_s: int
    edge_bps: int
    metadata: dict


async def fetch_trades(conn) -> list[TradeRow]:
    rows = await conn.fetch(
        """
        SELECT trade_idx, instrument, strategy_side,
               EXTRACT(EPOCH FROM entry_ts) AS entry_ts,
               entry_price, exit_price, pnl, t_in_window_s, edge_bps, metadata
        FROM research.backtest_trades
        WHERE backtest_id = $1::uuid
        ORDER BY entry_ts
        """,
        BACKTEST_ID,
    )
    out = []
    for r in rows:
        out.append(
            TradeRow(
                trade_idx=r["trade_idx"],
                slug=r["instrument"],
                strategy_side=r["strategy_side"],
                entry_ts=float(r["entry_ts"]),
                entry_price=float(r["entry_price"]),
                exit_price=float(r["exit_price"]),
                pnl=float(r["pnl"]),
                t_in_window_s=int(r["t_in_window_s"]),
                edge_bps=int(r["edge_bps"]),
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
            )
        )
    return out


async def fetch_market_state(conn, slug: str, entry_ts: float) -> dict:
    """Replicate paper/backtest_loader.py logic for a single market.

    Returns the open_price the loader would have produced (ohlcv 5m close
    at window_close-300), the last-tick chainlink + spot the driver would
    have used for settle, and the canonical Binance 1m close at the
    market's close_time (same path backfill_paper_settles.py uses).
    """
    first_tick = await conn.fetchrow(
        "SELECT window_close_ts FROM market_data.paper_ticks "
        "WHERE market_slug=$1 ORDER BY ts ASC LIMIT 1",
        slug,
    )
    if first_tick is None:
        return {"missing": True}
    window_close_ts = int(first_tick["window_close_ts"])
    # Loader: 5m candle close at ts = window_close - 300
    ohlcv5_row = await conn.fetchrow(
        "SELECT open, close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='5m' AND ts = to_timestamp($1)",
        window_close_ts - 300,
    )
    loader_open_price = (
        float(ohlcv5_row["close"]) if ohlcv5_row and ohlcv5_row["close"] is not None else None
    )
    # What the canonical strike SHOULD be: 1m close at exactly window_close-300
    # (= window_open). Same mechanism backfill_paper_settles uses for settle.
    ohlcv1_open_row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = to_timestamp($1)",
        window_close_ts - 300,
    )
    canonical_open = (
        float(ohlcv1_open_row["close"])
        if ohlcv1_open_row and ohlcv1_open_row["close"] is not None
        else None
    )
    # Driver final_price: last tick's chainlink (truthy) else spot
    last_tick = await conn.fetchrow(
        "SELECT spot_price, chainlink_price, t_in_window FROM market_data.paper_ticks "
        "WHERE market_slug=$1 ORDER BY ts DESC LIMIT 1",
        slug,
    )
    last_chainlink = float(last_tick["chainlink_price"]) if last_tick["chainlink_price"] else None
    last_spot = float(last_tick["spot_price"]) if last_tick["spot_price"] else 0.0
    last_t_in_window = (
        float(last_tick["t_in_window"]) if last_tick["t_in_window"] is not None else 0.0
    )
    driver_final_price = last_chainlink if last_chainlink else last_spot
    # Canonical settle: 1m close at window_close (same minute = market close_time)
    canonical_settle_row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = to_timestamp($1)",
        window_close_ts,
    )
    canonical_settle = (
        float(canonical_settle_row["close"])
        if canonical_settle_row and canonical_settle_row["close"] is not None
        else None
    )
    # Canonical 1m at window_close - 60 (close of last full minute before settle)
    canonical_settle_m1_row = await conn.fetchrow(
        "SELECT close FROM market_data.crypto_ohlcv "
        "WHERE exchange='binance' AND symbol='BTCUSDT' "
        "AND interval='1m' AND ts = to_timestamp($1)",
        window_close_ts - 60,
    )
    canonical_settle_m1 = (
        float(canonical_settle_m1_row["close"])
        if canonical_settle_m1_row and canonical_settle_m1_row["close"] is not None
        else None
    )
    # Spot at entry tick (closest paper_ticks row to entry_ts)
    entry_row = await conn.fetchrow(
        "SELECT spot_price, t_in_window FROM market_data.paper_ticks "
        "WHERE market_slug=$1 AND ts <= to_timestamp($2) "
        "ORDER BY ts DESC LIMIT 1",
        slug,
        entry_ts,
    )
    spot_at_entry = float(entry_row["spot_price"]) if entry_row and entry_row["spot_price"] else 0.0
    return {
        "missing": False,
        "window_close_ts": window_close_ts,
        "loader_open_price": loader_open_price,
        "canonical_open": canonical_open,
        "driver_final_price": driver_final_price,
        "last_chainlink": last_chainlink,
        "last_spot": last_spot,
        "last_t_in_window": last_t_in_window,
        "canonical_settle": canonical_settle,
        "canonical_settle_m1": canonical_settle_m1,
        "spot_at_entry": spot_at_entry,
    }


def driver_won(open_price: float, final_price: float, side_str: str) -> bool:
    if open_price <= 0 or final_price <= 0:
        return False
    went_up = final_price > open_price
    return went_up if side_str == "YES_UP" else not went_up


def canonical_won(open_p: float, settle_p: float, side_str: str) -> bool:
    if open_p <= 0 or settle_p <= 0:
        return False
    went_up = settle_p > open_p
    return went_up if side_str == "YES_UP" else not went_up


async def run() -> None:
    async with acquire() as conn:
        trades = await fetch_trades(conn)
        print(f"# {len(trades)} trades from backtest {BACKTEST_ID}\n")

        # Per-trade table
        rows = []
        n_resolution_match = 0
        n_canonical_disagree = 0
        n_canonical_yes_up_correct = 0
        n_loader_open_eq_close = 0
        n_chainlink_frozen_at_loader_open = 0
        edge_dist = []
        for t in trades:
            st = await fetch_market_state(conn, t.slug, t.entry_ts)
            if st["missing"]:
                continue
            stored_resolution = "win" if t.exit_price >= 0.5 else "loss"
            # What the driver would compute today
            driver_resolution = (
                "win"
                if driver_won(
                    st["loader_open_price"] or 0.0,
                    st["driver_final_price"] or 0.0,
                    t.strategy_side,
                )
                else "loss"
            )
            # Canonical resolution against Binance 1m
            canon_settle = st["canonical_settle"] or st["canonical_settle_m1"]
            canon_resolution = (
                "win"
                if canonical_won(
                    st["canonical_open"] or 0.0,
                    canon_settle or 0.0,
                    t.strategy_side,
                )
                else "loss"
            )
            if driver_resolution == stored_resolution:
                n_resolution_match += 1
            if canon_resolution != stored_resolution:
                n_canonical_disagree += 1
            if canon_resolution == "win":
                n_canonical_yes_up_correct += 1
            # Bug indicators
            if (
                st["loader_open_price"] is not None
                and st["last_chainlink"] is not None
                and abs(st["loader_open_price"] - st["last_chainlink"]) < 0.01
            ):
                n_chainlink_frozen_at_loader_open += 1
            edge_dist.append(t.edge_bps)

            rows.append(
                {
                    "idx": t.trade_idx,
                    "slug_close": st["window_close_ts"],
                    "side": t.strategy_side,
                    "stored": stored_resolution,
                    "driver_today": driver_resolution,
                    "canonical": canon_resolution,
                    "loader_open": st["loader_open_price"],
                    "canon_open": st["canonical_open"],
                    "driver_final": st["driver_final_price"],
                    "canon_settle": canon_settle,
                    "last_chainlink": st["last_chainlink"],
                    "last_spot": st["last_spot"],
                    "spot_at_entry": st["spot_at_entry"],
                    "t_in_win": t.t_in_window_s,
                    "edge_bps": t.edge_bps,
                    "pnl": t.pnl,
                }
            )

        # Print per-trade summary
        print(
            f"{'idx':>3} {'side':>9} {'stored':>6} {'driver':>6} {'canon':>6} "
            f"{'loader_open':>12} {'canon_open':>12} {'final':>10} {'settle':>10} "
            f"{'spot_in':>10} {'edgebps':>8} {'twin':>4}"
        )
        for r in rows:
            lo = f"{r['loader_open']:.2f}" if r["loader_open"] else "—"
            co = f"{r['canon_open']:.2f}" if r["canon_open"] else "—"
            df = f"{r['driver_final']:.2f}" if r["driver_final"] else "—"
            cs = f"{r['canon_settle']:.2f}" if r["canon_settle"] else "—"
            si = f"{r['spot_at_entry']:.2f}" if r["spot_at_entry"] else "—"
            print(
                f"{r['idx']:>3} {r['side']:>9} {r['stored']:>6} {r['driver_today']:>6} "
                f"{r['canonical']:>6} {lo:>12} {co:>12} {df:>10} {cs:>10} "
                f"{si:>10} {r['edge_bps']:>8} {r['t_in_win']:>4}"
            )

        n = len(rows)
        wins_stored = sum(1 for r in rows if r["stored"] == "win")
        wins_canonical = sum(1 for r in rows if r["canonical"] == "win")
        print()
        print(f"### Summary across {n} trades")
        print(f"stored win rate:        {wins_stored}/{n} = {wins_stored/n:.1%}")
        print(f"driver-today vs stored: {n_resolution_match}/{n} match (replay parity)")
        print(f"canonical (Binance 1m) win rate: {wins_canonical}/{n} = {wins_canonical/n:.1%}")
        print(f"canonical disagrees with stored: {n_canonical_disagree}/{n}")
        print(
            f"loader_open ≈ frozen chainlink: " f"{n_chainlink_frozen_at_loader_open}/{n} markets"
        )
        # Distribution of canonical-side correctness:
        # If canonical_won(canon_open, canon_settle, YES_UP) reflects truth,
        # then strategy's choice was right when canonical_resolution=='win'.
        sign_correct = sum(
            1 for r in rows if (r["canon_open"] and r["canon_settle"] and r["canonical"] == "win")
        )
        print(f"strategy direction was correct (canonical view): {sign_correct}/{n}")
        # delta_bps direction at entry vs side
        flips = 0
        for r in rows:
            if r["spot_at_entry"] and r["loader_open"]:
                delta = r["spot_at_entry"] - r["loader_open"]
                want_pos = r["side"] == "YES_UP"
                # Sign of delta should match want_pos given strategy gate
                if (delta > 0) != want_pos:
                    flips += 1
        print(f"side does not match sign(spot-loader_open) at entry: {flips}/{n}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())
