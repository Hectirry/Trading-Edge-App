"""Rolling 7-day k(δ) fill-intensity estimator (mm_rebate_v1 helpers).

Per-bucket, per-δ fill rate persisted across restarts in
`research.k_estimator_state` (init script `15_k_estimator_state.sql`).

Conceptually:
  k(bucket, δ) = fills_observed_at_or_within_delta(window_7d) / minutes_quoted(window_7d)

The strategy boots warm with the values committed during Step 0 (so the
first hour of paper has reasonable spread), then updates online as fills
arrive.

Step 0 v2 dependencies (TODO post-paper_ticks-15m)
--------------------------------------------------
- ``minutes_quoted`` accounting is approximate when sourced from
  polymarket_prices_history (1-min fidelity). Switch to true 1Hz wall-clock
  accounting from paper_ticks 15m once available.
- This module does NOT yet wire into the paper engine; the strategy class
  (Step 2) imports `KEstimator`, calls `record_fill` on each fill and
  `record_quoting_minute` periodically, and persists via `flush_to_db`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trading.common.db import acquire


@dataclass
class KCounter:
    """Counter for a single (bucket, delta_cents) cell.

    `fills_count` increments on each fill. `minutes_quoted` increments by
    `period_minutes` each time the strategy quotes in this bucket for that
    long. `k_value` is `fills_count / minutes_quoted` lazily computed.
    """

    fills_count: int = 0
    minutes_quoted: float = 0.0
    window_start: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def k(self) -> float:
        if self.minutes_quoted <= 0:
            return 0.0
        return self.fills_count / self.minutes_quoted


class KEstimator:
    """Per-strategy, per-bucket-per-δ k(δ) estimator with 7-day decay.

    Decay model: hard rolling window — when a `record_*` call observes that
    `now - window_start >= window_days`, the counters are reset and the
    `window_start` advances. This is simple and matches Step 0's batch
    methodology; a smoother exponential decay can replace it later without
    changing the public API.
    """

    def __init__(
        self,
        strategy_id: str,
        deltas_cents: tuple[int, ...] = (1, 2, 3, 5),
        window_days: int = 7,
    ) -> None:
        self.strategy_id = strategy_id
        self.deltas_cents = deltas_cents
        self.window = timedelta(days=window_days)
        self._counters: dict[tuple[str, int], KCounter] = {}

    def _maybe_roll(self, key: tuple[str, int], now: datetime) -> None:
        c = self._counters.get(key)
        if c is None:
            self._counters[key] = KCounter(window_start=now)
            return
        if (now - c.window_start) >= self.window:
            self._counters[key] = KCounter(window_start=now)

    def warm_start(self, bucket: str, delta_cents: int, k0: float, minutes: float = 60.0) -> None:
        """Seed the estimator with a Step 0 estimate so paper-deploy has a
        non-zero k for the first hour. `k0` is fills/min; we materialize
        `fills_count = round(k0 * minutes)` over `minutes` quoted.
        """
        key = (bucket, delta_cents)
        self._counters[key] = KCounter(
            fills_count=int(round(k0 * minutes)),
            minutes_quoted=minutes,
            window_start=datetime.now(tz=UTC),
        )

    def record_fill(self, bucket: str, delta_cents: int, now: datetime | None = None) -> None:
        """Increment the fill count for (bucket, δ). Called by the paper
        driver on each fill, with `delta_cents` the offset bucket the fill
        actually landed in (rounded UP to the nearest configured δ).
        """
        now = now or datetime.now(tz=UTC)
        if delta_cents not in self.deltas_cents:
            return
        key = (bucket, delta_cents)
        self._maybe_roll(key, now)
        self._counters[key].fills_count += 1

    def record_quoting_minute(
        self, bucket: str, delta_cents: int, period_minutes: float = 1.0, now: datetime | None = None
    ) -> None:
        """Increment minutes-quoted for (bucket, δ). Called by the paper
        driver every `period_minutes` while the strategy has live quotes
        in that bucket.
        """
        now = now or datetime.now(tz=UTC)
        if delta_cents not in self.deltas_cents:
            return
        key = (bucket, delta_cents)
        self._maybe_roll(key, now)
        self._counters[key].minutes_quoted += period_minutes

    def k(self, bucket: str, delta_cents: int) -> float:
        """Current estimated k(δ) for (bucket, δ) in fills/minute. Returns
        0 if the cell has no observations.
        """
        c = self._counters.get((bucket, delta_cents))
        return c.k() if c is not None else 0.0

    def snapshot(self) -> dict[tuple[str, int], dict]:
        """Materialize all counters for inspection / persistence."""
        return {
            key: {
                "fills_count": c.fills_count,
                "minutes_quoted": c.minutes_quoted,
                "k_value": c.k(),
                "window_start": c.window_start,
            }
            for key, c in self._counters.items()
        }

    async def flush_to_db(self) -> int:
        """Persist current counters to `research.k_estimator_state`.

        Idempotent: ON CONFLICT DO UPDATE refreshes counts, k_value, and
        last_update. Returns number of rows upserted. Async because the
        TEA paper driver is async-native.
        """
        rows = []
        now = datetime.now(tz=UTC)
        for (bucket, dc), c in self._counters.items():
            rows.append(
                (
                    self.strategy_id,
                    bucket,
                    dc,
                    c.window_start,
                    c.fills_count,
                    c.minutes_quoted,
                    c.k(),
                    now,
                )
            )
        if not rows:
            return 0
        async with acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO research.k_estimator_state (
                    strategy_id, bucket, delta_cents, window_start,
                    fills_count, minutes_quoted, k_value, last_update
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (strategy_id, bucket, delta_cents) DO UPDATE SET
                    window_start   = EXCLUDED.window_start,
                    fills_count    = EXCLUDED.fills_count,
                    minutes_quoted = EXCLUDED.minutes_quoted,
                    k_value        = EXCLUDED.k_value,
                    last_update    = EXCLUDED.last_update
                """,
                rows,
            )
        return len(rows)

    async def load_from_db(self) -> int:
        """Restore counters from `research.k_estimator_state` for this
        strategy_id. Used at boot. Returns number of cells loaded.
        """
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT bucket, delta_cents, window_start, fills_count, minutes_quoted
                FROM research.k_estimator_state WHERE strategy_id = $1
                """,
                self.strategy_id,
            )
        n = 0
        for r in rows:
            self._counters[(r["bucket"], r["delta_cents"])] = KCounter(
                fills_count=int(r["fills_count"]),
                minutes_quoted=float(r["minutes_quoted"]),
                window_start=r["window_start"],
            )
            n += 1
        return n
