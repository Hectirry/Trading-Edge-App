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


MM_INSTANCES = (
    "mm_rebate_v1_btc15m",
    "mm_rebate_v1_btc5m",
    "mm_rebate_v1_eth15m",
    "mm_rebate_v1_eth5m",
)


def _format(s: dict) -> str:
    base = (
        f"📊 Daily paper report — {s['day']}\n"
        f"Trades settled: {s['settles']}  (entries today: {s['entries']})\n"
        f"Win rate: {s['win_rate']*100:.1f}%  ({s['wins']}W / {s['losses']}L)\n"
        f"Total PnL: ${s['pnl']:+.2f}\n"
        f"Best trade: ${s['best_trade']:+.2f}\n"
        f"Worst trade: ${s['worst_trade']:+.2f}"
    )

    mm_instances: dict[str, dict | None] = s.get("mm_instances") or {}
    if any(mm_instances.values()):
        base += "\n\n— mm_rebate_v1 paper soak (multi-instance) —\n"
        # Cross-instance ranking table by net PnL / fill.
        rows = []
        for inst_name in MM_INSTANCES:
            mm = mm_instances.get(inst_name)
            if not mm:
                continue
            pnl_per_fill = (mm["taker_fee_paid_usdc"] * -1 + mm.get("net_pnl_usdc", 0)) / max(1, mm["fills"])
            rows.append(
                {
                    "name": inst_name.replace("mm_rebate_v1_", ""),
                    "fills": mm["fills"],
                    "ratio": mm["cancel_fill_ratio"],
                    "fee_paid": mm["taker_fee_paid_usdc"],
                    "markets": mm["markets_quoted"],
                    "pnl_per_fill": pnl_per_fill,
                }
            )
        rows.sort(key=lambda r: -r["pnl_per_fill"])
        base += f"{'instance':<10} {'fills':>6} {'cf':>5} {'fee$':>7} {'mkts':>5} {'$/fill':>8}\n"
        for r in rows:
            base += (
                f"{r['name']:<10} {r['fills']:>6} {r['ratio']:>5.1f} {r['fee_paid']:>7.2f} "
                f"{r['markets']:>5} {r['pnl_per_fill']:>+8.4f}\n"
            )

        # Per-instance alerts.
        alerts: list[str] = []
        for inst_name in MM_INSTANCES:
            mm = mm_instances.get(inst_name)
            if not mm:
                continue
            tag = inst_name.replace("mm_rebate_v1_", "")
            if mm.get("inventory_stuck_minutes", 0) > 30:
                alerts.append(f"⚠ {tag}: inventory stuck >{mm['inventory_stuck_minutes']:.0f}min")
            if mm.get("adverse_5s_max_bucket", 0) > 0.5:
                alerts.append(
                    f"⚠ {tag}: adverse_5s {mm['adverse_5s_max_bucket']:.2f} in {mm.get('adverse_worst_bucket', '?')}"
                )
            if mm.get("cancel_fill_ratio", 0) > 30:
                alerts.append(f"⚠ {tag}: cancel/fill {mm['cancel_fill_ratio']:.0f}:1 >30")
            if mm.get("intraday_drawdown_pct", 0) > 0.30:
                alerts.append(f"⚠ {tag}: drawdown {mm['intraday_drawdown_pct']*100:.0f}% >30%")
        if alerts:
            base += "\nALERTAS mm_rebate_v1:\n" + "\n".join(alerts)
    return base


async def _gather_mm_rebate_v1(day, strategy_id: str) -> dict | None:
    """Per-instance MM-style breakdown for mm_rebate_v1_<asset><horizon>.

    Returns None if no orders found for this strategy on this day (so the
    formatter can skip empty instances cleanly).
    """
    dt_from = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    dt_to = dt_from + timedelta(days=1)
    async with acquire() as conn:
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
                WHERE strategy_id=$3 AND ts_submit >= $1 AND ts_submit < $2
              )
              AND ts >= $1 AND ts < $2
            """,
            dt_from, dt_to, strategy_id,
        )
        markets_quoted = await conn.fetchval(
            """
            SELECT count(DISTINCT instrument_id) FROM trading.orders
            WHERE strategy_id=$3 AND ts_submit >= $1 AND ts_submit < $2
            """,
            dt_from, dt_to, strategy_id,
        )
        cancels_count = await conn.fetchval(
            """
            SELECT count(*) FROM trading.orders
            WHERE strategy_id=$3 AND status='CANCELLED'
              AND ts_submit >= $1 AND ts_submit < $2
            """,
            dt_from, dt_to, strategy_id,
        )
    fills = int(summary["total_fills"] or 0)
    if fills == 0 and (markets_quoted or 0) == 0:
        return None
    cancels = int(cancels_count or 0)
    ratio = (cancels / max(1, fills))
    return {
        "fills": fills,
        "cancels": cancels,
        "cancel_fill_ratio": ratio,
        "spread_capt_usdc": 0.0,         # owed Step 3
        "rebate_est_usdc": 0.0,          # owed Step 3
        "inv_pnl_usdc": 0.0,             # owed Step 3
        "net_pnl_usdc": 0.0,             # owed Step 3
        "taker_fee_paid_usdc": float(summary["fees_total"] or 0),
        "fills_by_bucket": {},           # owed Step 3
        "inv_peak_usdc": 0.0,             # owed Step 3
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
    # Per-instance mm_rebate_v1 breakdowns (4 instances).
    summary["mm_instances"] = {}
    for inst in MM_INSTANCES:
        try:
            summary["mm_instances"][inst] = await _gather_mm_rebate_v1(day, inst)
        except Exception as e:
            log.warning("%s.daily_report.err err=%s", inst, e)
            summary["mm_instances"][inst] = None
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
