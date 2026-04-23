"""Weekly A/B digest for the Moondev contest (ADR 0012).

Computes per-strategy accuracy + coverage over the last 7 days from
the paper fills table, two-proportion z-test vs a 0.50 baseline (no
edge), writes a row per arm into ``research.contest_ab_weekly``, and
sends a Telegram digest message on Sunday (or whenever the CLI is
invoked). Called manually or via the Phase 4 watcher cron.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

log = logging.getLogger("cli.contest_ab_weekly")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ARMS = ("contest_ensemble_v1", "contest_avengers_v1")
BASELINE_ACCURACY = 0.50
N_WINDOWS_PER_DAY = 288  # 5 m markets


async def _compute_arm(conn, strategy_id: str, since: datetime, until: datetime) -> dict:
    """Pull paper orders+settle fills for `strategy_id` in [since,until),
    return {n_predicted, n_correct, coverage, accuracy, ...}.
    """
    row = await conn.fetchrow(
        """
        WITH orders AS (
            SELECT o.order_id,
                   o.instrument_id,
                   o.side AS order_side,
                   f.metadata::jsonb->>'resolution' AS resolution
            FROM trading.orders o
            LEFT JOIN trading.fills f
              ON f.order_id = o.order_id
             AND f.metadata::jsonb->>'kind' = 'settle'
            WHERE o.mode = 'paper'
              AND o.strategy_id = $1
              AND o.ts_submit >= $2 AND o.ts_submit < $3
        )
        SELECT
            COUNT(*) FILTER (WHERE resolution IS NOT NULL) AS n_predicted,
            COUNT(*) FILTER (WHERE resolution = 'win') AS n_correct,
            COUNT(*) AS n_rows_total
        FROM orders;
        """,
        strategy_id, since, until,
    )
    n_predicted = int(row["n_predicted"] or 0)
    n_correct = int(row["n_correct"] or 0)
    n_total_windows_est = int(
        (until - since).total_seconds() / 60 / 5  # 5-minute markets
    )
    accuracy = (n_correct / n_predicted) if n_predicted else None
    coverage = (n_predicted / n_total_windows_est) if n_total_windows_est else None
    adjusted = (accuracy * coverage) if (accuracy and coverage) else None

    # Two-proportion z-test vs baseline. Only when n_predicted ≥ 30.
    p_value = None
    ci_lo = ci_hi = None
    if n_predicted >= 30:
        try:
            from statsmodels.stats.proportion import proportion_confint, proportions_ztest

            stat, p = proportions_ztest(
                count=n_correct, nobs=n_predicted, value=BASELINE_ACCURACY,
                alternative="two-sided",
            )
            p_value = float(p)
            ci_lo, ci_hi = proportion_confint(
                count=n_correct, nobs=n_predicted, alpha=0.05, method="wilson",
            )
        except Exception as e:
            log.warning("statsmodels.err", err=str(e))
    return {
        "n_predicted": n_predicted,
        "n_correct": n_correct,
        "n_windows_total": n_total_windows_est,
        "accuracy": accuracy,
        "coverage": coverage,
        "adjusted": adjusted,
        "p_value": p_value,
        "ci_lower": float(ci_lo) if ci_lo is not None else None,
        "ci_upper": float(ci_hi) if ci_hi is not None else None,
    }


async def _write_and_digest(
    week_start: datetime, week_end: datetime, results: list[tuple[str, dict]], *, quiet: bool,
) -> None:
    from trading.common.db import acquire

    async with acquire() as conn:
        for strategy_id, res in results:
            await conn.execute(
                """
                INSERT INTO research.contest_ab_weekly
                    (week_start, strategy_id, n_windows_total, n_predicted,
                     n_correct, accuracy, coverage, adjusted,
                     ci_lower, ci_upper, p_value_vs_baseline, details)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                ON CONFLICT (week_start, strategy_id) DO UPDATE SET
                    n_windows_total = EXCLUDED.n_windows_total,
                    n_predicted = EXCLUDED.n_predicted,
                    n_correct = EXCLUDED.n_correct,
                    accuracy = EXCLUDED.accuracy,
                    coverage = EXCLUDED.coverage,
                    adjusted = EXCLUDED.adjusted,
                    ci_lower = EXCLUDED.ci_lower,
                    ci_upper = EXCLUDED.ci_upper,
                    p_value_vs_baseline = EXCLUDED.p_value_vs_baseline,
                    details = EXCLUDED.details
                """,
                week_start, strategy_id,
                res["n_windows_total"], res["n_predicted"], res["n_correct"],
                res["accuracy"], res["coverage"], res["adjusted"],
                res["ci_lower"], res["ci_upper"], res["p_value"],
                json.dumps(res),
            )
    if quiet:
        return

    # Telegram digest.
    msg = _format_digest(week_start, week_end, results)
    log.info("digest:\n%s", msg)
    try:
        from trading.notifications import telegram as T

        client = T.TelegramClient()
        await client.send(T.AlertEvent(
            kind="CONTEST_AB_WEEKLY",
            text=msg,
            severity=T.Severity.INFO,
        ))
        await client.aclose()
    except Exception as e:
        log.warning("telegram.send_err", err=str(e))


def _format_digest(week_start, week_end, results) -> str:
    lines = [
        "📊 Contest A/B weekly",
        f"{week_start:%Y-%m-%d} → {week_end:%Y-%m-%d}",
        "",
    ]
    acc_by_arm: dict[str, float | None] = {}
    for strategy_id, res in results:
        acc = res["accuracy"]
        cov = res["coverage"]
        acc_by_arm[strategy_id] = acc
        lines.append(f"— {strategy_id}")
        lines.append(
            f"  predicted={res['n_predicted']}  correct={res['n_correct']}"
        )
        lines.append(
            f"  accuracy={_fmt_pct(acc)}  coverage={_fmt_pct(cov)}"
        )
        if res["ci_lower"] is not None:
            lines.append(
                f"  95% CI [{res['ci_lower']*100:.1f}%, {res['ci_upper']*100:.1f}%]"
            )
        if res["p_value"] is not None:
            lines.append(f"  p(vs 0.50) = {res['p_value']:.4f}")
    # Winner call
    a = acc_by_arm.get(ARMS[0])
    b = acc_by_arm.get(ARMS[1])
    if a is not None and b is not None:
        delta = a - b
        call = ARMS[0] if a > b else ARMS[1]
        lines.append("")
        lines.append(
            f"current leader: {call}  (Δacc={delta*100:+.1f} pp)"
        )
    return "\n".join(lines)


def _fmt_pct(v):
    return "-" if v is None else f"{v*100:.1f}%"


async def main_async(args):
    now = datetime.now(tz=UTC)
    week_end = now
    week_start = week_end - timedelta(days=7)
    if args.week_start:
        week_start = datetime.fromisoformat(args.week_start).replace(tzinfo=UTC)
        week_end = week_start + timedelta(days=7)

    from trading.common.db import acquire, close_pool

    results: list[tuple[str, dict]] = []
    async with acquire() as conn:
        for strategy_id in ARMS:
            res = await _compute_arm(conn, strategy_id, week_start, week_end)
            results.append((strategy_id, res))
    await _write_and_digest(week_start, week_end, results, quiet=args.quiet)
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-start", default=None,
                    help="ISO datetime (UTC); defaults to now - 7d.")
    ap.add_argument("--quiet", action="store_true",
                    help="Skip Telegram send; still writes DB row.")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
