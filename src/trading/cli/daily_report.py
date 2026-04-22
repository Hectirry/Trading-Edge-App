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
    return (
        f"📊 Daily paper report — {s['day']}\n"
        f"Trades settled: {s['settles']}  (entries today: {s['entries']})\n"
        f"Win rate: {s['win_rate']*100:.1f}%  ({s['wins']}W / {s['losses']}L)\n"
        f"Total PnL: ${s['pnl']:+.2f}\n"
        f"Best trade: ${s['best_trade']:+.2f}\n"
        f"Worst trade: ${s['worst_trade']:+.2f}"
    )


async def _run(args) -> None:
    if args.date:
        day = datetime.fromisoformat(args.date).date()
    else:
        day = (datetime.now(tz=UTC) - timedelta(days=1)).date()
    summary = await _gather(day)
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
