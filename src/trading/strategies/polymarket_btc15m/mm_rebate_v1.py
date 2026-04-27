"""mm_rebate_v1 — inventory-neutral Avellaneda-Stoikov market maker on
Polymarket BTC-updown 15m, restricted to below-zona buckets [0.15, 0.40].

Strategy class for Step 2. Direct paper deploy without shadow mode per
operator decision (aggressive paper soak to learn the regime). All
gates, caps, and parameters come from
`config/strategies/polymarket_btc15m_mm_rebate_v1.toml`.

Behavior per tick (`on_tick(ctx) -> list[MMAction]`):
  1. Skip if outside the active price zone (parametrizable; V1 = [0.15, 0.40]).
  2. Skip if inside the dead zone (parametrizable; default [0.40, 0.60]).
  3. Skip if outside the quoting time window (default [60s, T-30s]).
  4. Skip if the market is killed by MMSafetyGuard (cancel/fill).
  5. Compute σ_BM(t), reservation price, optimal bid/ask via _mm_features.
  6. Inventory cap check via MMSafetyGuard.block_post_quote.
  7. Emit either a fresh PostQuote pair (no live quotes) or
     ReplaceQuote pair (existing quotes need to move with the book).

The strategy maintains its own per-market resting-quote ledger
(`_live_quotes_by_market`) so it can decide whether to post fresh,
replace, or cancel as the book moves.

Step 0 v2 dependencies (TODO post-paper_ticks-15m)
--------------------------------------------------
- `_compute_p_fair`: currently uses `ctx.implied_prob_yes` (live mid from
  CLOB WS, available even pre-paper_ticks-15m). With paper_ticks 15m
  populated, validate against the 1Hz mid time series and consider
  using a smoothed estimator.
- `k_estimator.warm_start` per-bucket values come from Step 0 v1; Step 0
  v2 re-warms with corrected methodology.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from trading.common.logging import get_logger
from trading.engine.mm_actions import CancelQuote, MMAction, PostQuote, ReplaceQuote
from trading.engine.mm_safety import MMSafetyGuard, MMSafetyParams
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Side, TickContext
from trading.strategies.polymarket_btc15m._k_estimator import KEstimator
from trading.strategies.polymarket_btc15m._mm_features import (
    ASParams,
    in_active_zone,
    in_quoting_window,
    optimal_bid_ask,
)

log = get_logger("strategy.mm_rebate_v1")


@dataclass
class _LiveQuote:
    """Local ledger of a posted quote so we can decide replace-vs-skip."""

    client_order_id: str
    side: Side
    price: float
    qty_shares: float


def _bucket_of(p: float, edges: list[tuple[float, float, str]]) -> str | None:
    for lo, hi, name in edges:
        if lo <= p < hi:
            return name
    return None


def _delta_cents_round_up(offset: float, deltas: tuple[int, ...]) -> int:
    """Round `offset` (YES-price units) UP to the nearest configured δ in cents."""
    cents = max(1, int(round(offset * 100, 0)))
    for d in deltas:
        if cents <= d:
            return d
    return deltas[-1]


def _coid(strategy: str, slug: str, ts: float, side: Side, seed: str) -> str:
    key = f"{strategy}|{slug}|{ts:.6f}|{side.value}|{seed}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


class MMRebateV1(StrategyBase):
    name = "mm_rebate_v1"

    BUCKET_EDGES: list[tuple[float, float, str]] = [
        (0.15, 0.20, "0.15-0.20"),
        (0.20, 0.30, "0.20-0.30"),
        (0.30, 0.40, "0.30-0.40"),
    ]

    def __init__(self, config: dict, k_estimator: KEstimator | None = None) -> None:
        super().__init__(config)
        p = self.params

        self.zone_lo = float(p.get("zone_lo", 0.15))
        self.zone_hi = float(p.get("zone_hi", 0.40))
        self.dead_lo = float(p.get("dead_zone_lo", 0.40))
        self.dead_hi = float(p.get("dead_zone_hi", 0.60))
        self.entry_window_start_s = float(p.get("entry_window_start_s", 60.0))
        self.stake_nominal_usd = float(p.get("stake_nominal_usd", 20.0))

        self.as_params = ASParams(
            gamma_inventory_risk=float(p.get("gamma_inventory_risk", 0.5)),
            window_seconds=int(p.get("window_seconds", 900)),
            spread_floor_bps=float(p.get("spread_floor_bps", 50.0)),
            spread_ceiling_bps=float(p.get("spread_ceiling_bps", 500.0)),
            tau_terminal_s=float(p.get("tau_terminal_s", 30.0)),
        )

        mm_safety_cfg = config.get("mm_safety", {})
        self.safety = MMSafetyGuard(
            strategy_id=self.name,
            params=MMSafetyParams(
                inventory_cap_usdc=float(mm_safety_cfg.get("inventory_cap_usdc", 50.0)),
                cancel_fill_ratio_max=float(mm_safety_cfg.get("cancel_fill_ratio_max", 30.0)),
                cancel_fill_window_minutes=int(mm_safety_cfg.get("cancel_fill_window_minutes", 5)),
                cancel_fill_kill_threshold=int(mm_safety_cfg.get("cancel_fill_kill_threshold", 2)),
                cancel_fill_kill_window_min=int(mm_safety_cfg.get("cancel_fill_kill_window_min", 60)),
                cancel_fill_resume_min=int(mm_safety_cfg.get("cancel_fill_resume_min", 15)),
                taker_fee_canary_pct=float(mm_safety_cfg.get("taker_fee_canary_pct", 0.05)),
                taker_fee_canary_window_days=int(mm_safety_cfg.get("taker_fee_canary_window_days", 7)),
                tau_terminal_s=float(mm_safety_cfg.get("tau_terminal_s", 30.0)),
                auto_kill_on_breach=bool(mm_safety_cfg.get("auto_kill_on_breach", False)),
            ),
        )

        self.k = k_estimator or KEstimator(strategy_id=self.name)

        # Per-market live quotes (one bid + one ask at most in V1).
        self._live_quotes_by_market: dict[str, list[_LiveQuote]] = {}
        # ttl seconds for emitted quotes. 0 = GTC, otherwise driver auto-cancels.
        self.ttl_seconds = int(p.get("quote_ttl_seconds", 0))

    # ─── public interface ────────────────────────────────────────────

    def on_tick(self, ctx: TickContext) -> list[MMAction]:
        actions: list[MMAction] = []
        slug = ctx.market_slug

        # Stage 1: zone gates
        p_fair = self._compute_p_fair(ctx)
        if not in_active_zone(p_fair, self.zone_lo, self.zone_hi, self.dead_lo, self.dead_hi):
            return self._cancel_all_for_market(slug, reason="out_of_zone")

        # Stage 2: time gate (driver-level skip + tau_terminal handled by guard)
        if not in_quoting_window(ctx.t_in_window, self.as_params, self.entry_window_start_s):
            return self._cancel_all_for_market(slug, reason="out_of_quoting_window")

        # Stage 3: market killed by safety
        if self.safety.is_market_killed(slug):
            return self._cancel_all_for_market(slug, reason="market_killed_by_safety")

        # Stage 4: terminal-window skip
        block_terminal, _ = self.safety.block_post_quote_terminal(
            ctx.t_in_window, self.as_params.window_seconds
        )
        if block_terminal:
            return self._cancel_all_for_market(slug, reason="tau_terminal")

        # Stage 5: derive optimal bid/ask
        bucket = _bucket_of(p_fair, self.BUCKET_EDGES) or "0.15-0.20"
        k_at_2c = self.k.k(bucket, 2)
        q_shares = self.safety.inventory_shares(slug)
        bid, ask = optimal_bid_ask(p_fair, q_shares, k_at_2c, self.as_params, ctx.t_in_window)

        qty_shares = self.stake_nominal_usd / max(bid, 0.001)

        # Stage 6: per-side inventory cap projection
        actions.extend(
            self._post_or_replace_side(slug, Side.YES_UP, bid, qty_shares, p_fair, ctx)
        )
        actions.extend(
            self._post_or_replace_side(slug, Side.YES_DOWN, ask, qty_shares, p_fair, ctx)
        )

        return actions

    def on_fill(
        self,
        *,
        market_slug: str,
        client_order_id: str,
        side: str,
        fill_price: float,
        fill_qty_shares: float,
        ts: float,
    ) -> None:
        # Update inventory (YES_UP = bid filled = bought YES = +q;
        # YES_DOWN = ask filled = sold YES = -q).
        side_sign = 1 if side == Side.YES_UP.value else -1
        self.safety.record_inventory_after_fill(market_slug, side_sign, fill_qty_shares)
        self.safety.record_fill(market_slug)
        # Drop the filled quote from the local ledger; the driver will
        # not know whether to repost until the next tick.
        live = self._live_quotes_by_market.get(market_slug, [])
        self._live_quotes_by_market[market_slug] = [
            q for q in live if q.client_order_id != client_order_id
        ]
        # Update the k_estimator at the δ bin closest to the fill.
        # We use the offset from the contemporaneous mid (best estimate
        # is fill_price itself, post-fill it's ≈ mid).
        self.k.record_fill(self._bucket_for_estimator(fill_price), 2)

    # ─── private helpers ─────────────────────────────────────────────

    def _compute_p_fair(self, ctx: TickContext) -> float:
        """Brownian-bridge mid prior. V1 uses the implied_prob_yes from CLOB
        WS (== live mid) directly. Future: smooth via EWMA + spot anchor.
        """
        return float(ctx.implied_prob_yes)

    def _cancel_all_for_market(self, market_slug: str, reason: str) -> list[MMAction]:
        live = self._live_quotes_by_market.get(market_slug, [])
        out = [
            CancelQuote(
                client_order_id=q.client_order_id, market_slug=market_slug, reason=reason
            )
            for q in live
        ]
        if out:
            for q in live:
                self.safety.record_cancel(market_slug)
            self._live_quotes_by_market[market_slug] = []
        return out

    def _post_or_replace_side(
        self,
        slug: str,
        side: Side,
        price: float,
        qty_shares: float,
        p_fair: float,
        ctx: TickContext,
    ) -> list[MMAction]:
        side_sign = 1 if side is Side.YES_UP else -1
        blocked, _ = self.safety.block_post_quote(slug, side_sign, qty_shares, p_fair)
        if blocked:
            return self._cancel_side(slug, side, reason="inventory_cap")

        existing = self._existing_for(slug, side)
        seed = "bid" if side is Side.YES_UP else "ask"
        new_coid = _coid(self.name, slug, ctx.ts, side, seed)
        new_quote = PostQuote(
            side=side,
            price=price,
            qty_shares=qty_shares,
            market_slug=slug,
            ttl_seconds=self.ttl_seconds,
            client_id_seed=seed,
        )

        if existing is None:
            self._track_new(slug, new_coid, side, price, qty_shares)
            return [new_quote]

        # Replace if price moved more than 1¢ from the live quote.
        if abs(existing.price - price) >= 0.01:
            self._untrack(slug, existing.client_order_id)
            self._track_new(slug, new_coid, side, price, qty_shares)
            self.safety.record_cancel(slug)
            return [ReplaceQuote(old_client_order_id=existing.client_order_id, new=new_quote)]

        # Quote still good; do nothing.
        return []

    def _existing_for(self, slug: str, side: Side) -> _LiveQuote | None:
        for q in self._live_quotes_by_market.get(slug, []):
            if q.side is side:
                return q
        return None

    def _cancel_side(self, slug: str, side: Side, reason: str) -> list[MMAction]:
        existing = self._existing_for(slug, side)
        if existing is None:
            return []
        self._untrack(slug, existing.client_order_id)
        self.safety.record_cancel(slug)
        return [
            CancelQuote(
                client_order_id=existing.client_order_id,
                market_slug=slug,
                reason=reason,
            )
        ]

    def _track_new(
        self, slug: str, coid: str, side: Side, price: float, qty_shares: float
    ) -> None:
        self._live_quotes_by_market.setdefault(slug, []).append(
            _LiveQuote(
                client_order_id=coid, side=side, price=price, qty_shares=qty_shares
            )
        )

    def _untrack(self, slug: str, coid: str) -> None:
        live = self._live_quotes_by_market.get(slug, [])
        self._live_quotes_by_market[slug] = [q for q in live if q.client_order_id != coid]

    def _bucket_for_estimator(self, p: float) -> str:
        return _bucket_of(p, self.BUCKET_EDGES) or "0.15-0.20"
