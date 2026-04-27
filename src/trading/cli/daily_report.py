"""Daily paper-trading report → Telegram.

Cron schedule: 00:05 UTC in the tea-telegram-bot container.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import configure_logging, get_logger
from trading.notifications import telegram as T

log = get_logger("cli.daily_report")


async def _gather(day: date) -> dict:
    dt_from = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    dt_to = dt_from + timedelta(days=1)
    async with acquire() as conn:
        summary = await conn.fetchrow(
            """
            WITH entries AS (
                SELECT fill_id, order_id, ts, price, fee
                FROM trading.fills
                WHERE mode='paper' AND metadata::jsonb->>'kind'='entry'
                  AND ts >= $1 AND ts < $2
            ),
            settles AS (
                SELECT order_id, price AS exit_price,
                       (metadata::jsonb->>'pnl')::numeric AS pnl,
                       metadata::jsonb->>'resolution' AS resolution
                FROM trading.fills
                WHERE mode='paper' AND metadata::jsonb->>'kind'='settle'
                  AND ts >= $1 AND ts < $2
            )
            SELECT
                (SELECT count(*) FROM entries) AS entries,
                (SELECT count(*) FROM settles) AS settles,
                (SELECT count(*) FROM settles WHERE resolution='win') AS wins,
                (SELECT count(*) FROM settles WHERE resolution='loss') AS losses,
                COALESCE((SELECT sum(pnl) FROM settles), 0) AS pnl,
                COALESCE((SELECT min(pnl) FROM settles), 0) AS worst,
                COALESCE((SELECT max(pnl) FROM settles), 0) AS best
            """,
            dt_from,
            dt_to,
        )
    n = int(summary["settles"] or 0)
    wins = int(summary["wins"] or 0)
    return {
        "day": day.isoformat(),
        "entries": int(summary["entries"] or 0),
        "settles": n,
        "wins": wins,
        "losses": int(summary["losses"] or 0),
        "win_rate": (wins / n) if n else 0.0,
        "pnl": float(summary["pnl"] or 0),
        "best_trade": float(summary["best"] or 0),
        "worst_trade": float(summary["worst"] or 0),
    }


def _format(s: dict) -> str:
    base = (
        f"📊 Daily paper report — {s['day']}\n"
        f"Trades settled: {s['settles']}  (entries today: {s['entries']})\n"
        f"Win rate: {s['win_rate']*100:.1f}%  ({s['wins']}W / {s['losses']}L)\n"
        f"Total PnL: ${s['pnl']:+.2f}\n"
        f"Best trade: ${s['best_trade']:+.2f}\n"
        f"Worst trade: ${s['worst_trade']:+.2f}"
    )
    mm = s.get("mm_rebate_v1")
    if mm:
        base += "\n\n— mm_rebate_v1 (paper-aggressive soak) —\n"
        base += (
            f"Maker fills: {mm['fills']}  cancels: {mm['cancels']}  ratio: {mm['cancel_fill_ratio']:.1f}\n"
            f"PnL desglose:\n"
            f"  spread captured : ${mm['spread_capt_usdc']:+.2f}\n"
            f"  rebate (est)    : ${mm['rebate_est_usdc']:+.2f}\n"
            f"  inventory P&L   : ${mm['inv_pnl_usdc']:+.2f}\n"
            f"  taker fee paid  : ${mm['taker_fee_paid_usdc']:.2f}\n"
            f"Fills por bucket: {mm['fills_by_bucket']}\n"
            f"Inventory peak  : ${mm['inv_peak_usdc']:.2f} (cap ${mm['inv_cap_usdc']:.0f})\n"
            f"Markets quoted  : {mm['markets_quoted']}\n"
            f"Top 3 winners   : {mm['top_winners']}\n"
            f"Top 3 losers    : {mm['top_losers']}"
        )
        # 4 alert conditions per operator — fire if breached.
        alerts: list[str] = []
        if mm.get("inventory_stuck_minutes", 0) > 30:
            alerts.append(f"⚠ inventory stuck at cap >{mm['inventory_stuck_minutes']:.0f}min")
        if mm.get("adverse_5s_max_bucket", 0) > 0.5:
            alerts.append(
                f"⚠ adverse_5s ratio {mm['adverse_5s_max_bucket']:.2f} in bucket {mm.get('adverse_worst_bucket', '?')}"
            )
        if mm.get("cancel_fill_ratio", 0) > 30:
            alerts.append(f"⚠ cancel/fill ratio {mm['cancel_fill_ratio']:.0f}:1 (>30:1 alert)")
        if mm.get("intraday_drawdown_pct", 0) > 0.30:
            alerts.append(
                f"⚠ intraday drawdown {mm['intraday_drawdown_pct']*100:.0f}% > 30% of capital_at_risk"
            )
        if alerts:
            base += "\n\nALERTAS mm_rebate_v1:\n" + "\n".join(alerts)
    return base


async def _gather_mm_rebate_v1(day) -> dict:
    """Per-strategy MM-style breakdown for mm_rebate_v1.

    Reads from trading.fills filtered by strategy_id='mm_rebate_v1'. The
    schema decomposition (spread vs rebate vs inventory P&L) is owed —
    Step 0 v2 will refine. V1 ships a coarse decomposition based on
    fill_metadata + maker_fee_bps from limit_book_sim.
    """
    dt_from = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    dt_to = dt_from + timedelta(days=1)
    async with acquire() as conn:
        # Total fills by liquidity_side + sum(fee). MAKER fee is the maker
        # fee from limit_book_sim (10 bps default). Inventory P&L not yet
        # in fills metadata — owed.
        summary = await conn.fetchrow(
            """
            SELECT
              count(*)                  AS total_fills,
              count(*) FILTER (WHERE liquidity_side='MAKER')  AS maker_fills,
              count(*) FILTER (WHERE liquidity_side='TAKER')  AS taker_fills,
              COALESCE(SUM(price * qty), 0) AS notional,
              COALESCE(SUM(fee), 0)     AS fees_total
            FROM trading.fills
            WHERE mode='paper'
              AND order_id IN (
                SELECT order_id FROM trading.orders
                WHERE strategy_id='mm_rebate_v1' AND ts_submit >= $1 AND ts_submit < $2
              )
              AND ts >= $1 AND ts < $2
            """,
            dt_from,
            dt_to,
        )
        markets_quoted = await conn.fetchval(
            """
            SELECT count(DISTINCT instrument_id) FROM trading.orders
            WHERE strategy_id='mm_rebate_v1' AND ts_submit >= $1 AND ts_submit < $2
            """,
            dt_from,
            dt_to,
        )
    fills = int(summary["total_fills"] or 0)
    cancels = 0  # owed: count from trading.orders WHERE status='CANCELLED' filtered by strategy
    ratio = (cancels / max(1, fills))
    return {
        "fills": fills,
        "cancels": cancels,
        "cancel_fill_ratio": ratio,
        "spread_capt_usdc": 0.0,         # owed: derive from fill_price vs mid_at_fill
        "rebate_est_usdc": 0.0,          # owed: query rebate_pool_share once aggregated
        "inv_pnl_usdc": 0.0,             # owed: position_snapshots × mid drift
        "taker_fee_paid_usdc": float(summary["fees_total"] or 0),
        "fills_by_bucket": {},           # owed
        "inv_peak_usdc": 0.0,            # owed: position_snapshots
        "inv_cap_usdc": 50.0,
        "markets_quoted": int(markets_quoted or 0),
        "top_winners": [],
        "top_losers": [],
        "inventory_stuck_minutes": 0,
        "adverse_5s_max_bucket": 0.0,
        "adverse_worst_bucket": None,
        "intraday_drawdown_pct": 0.0,
    }


async def _run(args) -> None:
    if args.date:
        day = datetime.fromisoformat(args.date).date()
    else:
        day = (datetime.now(tz=UTC) - timedelta(days=1)).date()
    summary = await _gather(day)
    # mm_rebate_v1 sub-section. Coarse v1 — Step 0 v2 / Step 3 refine.
    try:
        summary["mm_rebate_v1"] = await _gather_mm_rebate_v1(day)
    except Exception as e:
        log.warning("mm_rebate_v1.daily_report.err", err=str(e))
        summary["mm_rebate_v1"] = None
    msg = _format(summary)
    log.info("daily_report.ready", **summary)
    if args.print_only:
        print(msg)
        return
    tg = T.TelegramClient()
    # Bypass dedupe: daily report is expected once per day, distinct kind.
    await tg.send(T.AlertEvent(kind=f"DAILY_REPORT_{day.isoformat()}", text=msg))
    await tg.aclose()


def main() -> None:
    configure_logging()
    _ = get_settings()  # ensure env pulled
    p = argparse.ArgumentParser(prog="trading.cli.daily_report")
    p.add_argument("--date", default=None, help="YYYY-MM-DD; default = yesterday UTC")
    p.add_argument("--print-only", action="store_true", help="skip Telegram send")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
