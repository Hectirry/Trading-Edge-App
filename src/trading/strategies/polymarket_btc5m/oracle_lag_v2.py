"""oracle_lag_v2 — maker-first quoting on the BS-digital residual.

Sister of ``oracle_lag_v1``. **Same scoring core** ``Φ(δ/σ√τ)``, **same
cesta provider**. The difference is the execution policy:

- v1 (taker)  → at ``t ∈ [285, 297]`` fire FAK at the ask. Pays the
  Polymarket dynamic taker fee 1.5-3 % per share.
- v2 (maker)  → from ``t ≈ 60 s`` onward post a GTC limit at the
  Avellaneda-Stoikov optimal offset on the maker side. Cancel + re-quote
  on book moves or EV decay. Captures the 0 % maker rebate instead of
  paying the taker fee.

Why a wider entry window: the maker can afford to camp for a fill —
v1's tight 285-297 s window was driven by needing fee-favoured ask
prices in the last few seconds, where taker direction is
near-deterministic. v2 doesn't pay the fee and benefits from longer
queue residency, so we open the window at T-240s (``t = 60 s``) by
default. The same EV gate ``EV_neto > θ`` decides whether to quote at
all; below threshold we SKIP and re-evaluate next tick.

Cancel / re-quote — important constraint: the strategy ``should_enter``
hot-path is sync 1 Hz (``StrategyBase``). It does NOT manage orders
directly; it returns a ``Decision`` with ``order_type=GTC`` and a
``limit_price`` and the executor handles placement / cancellation. The
ADR 0014 cancel-on-edge-drop logic is therefore implemented at the
strategy level by tracking the *last quoted limit price* and EV per
market, and emitting an updated ``Decision`` when the quote needs
revision. The driver / executor is responsible for translating the
``Decision`` sequence into actual cancel + place pairs (a small policy
adapter on top of ``SimulatedExecutionClient``; see TODO at bottom).

Rate-limit budget: Polymarket allows 3,000 cancels / 10 s per account;
with the per-strategy clamp ``max_active_quotes_per_market = 1`` and
``cancel_min_interval_s ≥ 2`` we stay at ≤ 30 cancels / 60 s per
strategy regardless of book turbulence.

Shadow gate: defaults to ``[paper] shadow = true`` until the operator
explicitly flips it after the v1 vs v2 PnL/share gate (ADR 0014
falsification: ≥ +1.5 ¢/share over the same paper_ticks window).
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.common.logging import get_logger
from trading.engine.avellaneda_stoikov import quote as as_quote
from trading.engine.features.black_scholes_digital import p_up, sigma_ewma
from trading.engine.strategy_base import StrategyBase
from trading.engine.types import Action, Decision, OrderType, Side, TickContext
from trading.strategies.polymarket_btc5m._oracle_lag_cesta import CestaProvider

log = get_logger("strategy.oracle_lag_v2")


@dataclass
class _ActiveQuote:
    """Per-market state to drive the cancel/re-quote decision.

    The strategy does not touch the book — these fields are read by the
    next ``should_enter`` tick to decide whether to emit ENTER again
    (which the executor interprets as cancel-old / place-new) or SKIP.
    """

    side: Side
    limit_price: float
    quoted_ev_net: float
    quoted_ts: float
    last_action_ts: float = 0.0  # rate-limit anchor (cancel + replace)
    cancel_count: int = 0


def _maker_fee(_p: float) -> float:
    """Maker side on Polymarket charges 0 % (no rebate as of 2026 Q2).

    Returned as a function for symmetry with v1's ``_dynamic_fee``;
    when Polymarket rolls out maker rebates this becomes a negative
    number and the EV gate widens accordingly. See ADR 0014 §3.
    """
    return 0.0


class OracleLagV2(StrategyBase):
    name = "oracle_lag_v2"

    def __init__(self, config: dict, cesta: CestaProvider | None = None) -> None:
        super().__init__(config)
        p = self.params
        # Wider entry window: open at t = 60 s by default (T-240s); close
        # at t = 297 s (same as v1). Maker camps the book for a fill.
        self.entry_start_s = float(p.get("entry_window_start_s", 60.0))
        self.entry_end_s = float(p.get("entry_window_end_s", 297.0))
        self.sigma_lookback_s = float(p.get("sigma_lookback_s", 90.0))
        self.sigma_min_ticks = int(p.get("sigma_min_ticks", 60))
        self.ewma_lambda = float(p.get("ewma_lambda", 0.94))
        # No taker fee model — maker side. ev_threshold is gross of fee.
        self.ev_threshold = float(p.get("ev_threshold", 0.005))
        self.usdt_basis_phase0 = float(p.get("usdt_basis_phase0", 1.0))

        # Avellaneda-Stoikov execution params (read from [execution] in TOML).
        ex = config.get("execution", {})
        self.gamma_inventory = float(ex.get("gamma_inventory", 0.1))
        self.k_order_arrival = float(ex.get("k_order_arrival", 5.0))
        self.limit_offset_bps = float(ex.get("limit_offset_bps", 50.0))
        self.cancel_threshold_drop_bps = float(
            ex.get("cancel_threshold_drop_bps", 30.0)
        )
        self.cancel_min_interval_s = float(ex.get("cancel_min_interval_s", 2.0))
        self.max_active_quotes_per_market = int(
            ex.get("max_active_quotes_per_market", 1)
        )
        self.mode = str(ex.get("mode", "maker"))
        if self.mode != "maker":
            log.warning(
                "oracle_lag_v2.mode_not_maker",
                mode=self.mode,
                note="v2 only supports maker; falling back will run as taker-equivalent",
            )

        self.cesta = cesta
        self._active: dict[str, _ActiveQuote] = {}
        self._per_window_filled: set[str] = set()

    def on_start(self) -> None:
        self._active.clear()
        self._per_window_filled.clear()

    # ----- helpers -----
    def _ev_with_limit(
        self, *, p_side: float, limit_price: float
    ) -> tuple[float, float]:
        """EV of buying 1 share at ``limit_price`` of the favoured side.

        Maker fee = 0. Returns (ev_gross, ev_net). The two are equal
        for v2; kept as a 2-tuple for parity with v1's signal_features.

        Edge cases handled by caller (limit ≤ 0 or ≥ 1 are rejected
        upstream).
        """
        ev_gross = p_side * (1.0 - limit_price) - (1.0 - p_side) * limit_price
        ev_net = ev_gross - _maker_fee(limit_price)
        return ev_gross, ev_net

    def _book_mid(self, ctx: TickContext, side: Side) -> float | None:
        """Return the mid price of the favoured outcome's book.

        Polymarket quotes YES and NO independently; for v2 we pick the
        side we want exposure to (YES_UP = buy YES; YES_DOWN = buy NO)
        and use that side's bid/ask mid.
        """
        if side == Side.YES_UP:
            bid, ask = ctx.pm_yes_bid, ctx.pm_yes_ask
        else:
            bid, ask = ctx.pm_no_bid, ctx.pm_no_ask
        if bid <= 0 or ask <= 0 or bid >= 1.0 or ask >= 1.0 or ask <= bid:
            return None
        return 0.5 * (bid + ask)

    # ----- main entry point -----
    def should_enter(self, ctx: TickContext) -> Decision:
        # 1. Wider entry window than v1.
        if not (self.entry_start_s <= ctx.t_in_window <= self.entry_end_s):
            return Decision(Action.SKIP, reason="outside_entry_window")

        # 2. If this market already filled, do nothing further this window.
        if ctx.market_slug in self._per_window_filled:
            return Decision(Action.SKIP, reason="already_filled")

        if ctx.open_price <= 0 or ctx.spot_price <= 0:
            return Decision(Action.SKIP, reason="bad_price_data")

        # 3. Cesta-weighted P_spot (same as v1 — reuses CestaProvider).
        cesta_dbg: dict | None = None
        if self.cesta is not None:
            p_spot_usd, cesta_dbg = self.cesta.p_spot(ctx.ts, ctx.spot_price)
        else:
            p_spot_usd = ctx.spot_price / self.usdt_basis_phase0
        delta_pct = (p_spot_usd - ctx.open_price) / ctx.open_price

        # 4. σ EWMA (same module as v1).
        cutoff_ts = ctx.ts - self.sigma_lookback_s
        spots = [
            t.spot_price
            for t in ctx.recent_ticks
            if t.ts >= cutoff_ts and t.spot_price > 0
        ]
        spots.append(ctx.spot_price)
        if len(spots) < self.sigma_min_ticks:
            return Decision(Action.SKIP, reason="insufficient_sigma_ticks")
        sigma = sigma_ewma(spots, lam=self.ewma_lambda)
        if sigma <= 0:
            return Decision(Action.SKIP, reason="sigma_collapsed")

        tau = max(0.0, ctx.window_close_ts - ctx.ts)
        prob_up = p_up(delta_pct=delta_pct, tau_s=tau, sigma_per_sqrt_s=sigma)

        # 5. Side selection from p_up (identical to v1).
        if prob_up >= 0.5:
            side = Side.YES_UP
            ask = ctx.pm_yes_ask
            p_side = prob_up
        else:
            side = Side.YES_DOWN
            ask = ctx.pm_no_ask
            p_side = 1.0 - prob_up

        if ask <= 0 or ask >= 1.0:
            return Decision(Action.SKIP, reason="bad_ask")

        # 6. Avellaneda-Stoikov optimal limit price. The mid of the
        # favoured outcome book is the "fair" price; we post the BUY
        # side at AS-optimal bid offset. ``q = 0`` because v2 takes one
        # position per market, no rolling inventory.
        mid = self._book_mid(ctx, side)
        if mid is None:
            return Decision(Action.SKIP, reason="bad_book_mid")

        as_q = as_quote(
            mid_price=mid,
            sigma_per_sqrt_s=sigma,
            tau_s=tau,
            gamma=self.gamma_inventory,
            k=self.k_order_arrival,
            inventory=0.0,
        )
        # Maker BUY: post below the mid, at AS bid_price; clamp by
        # ``limit_offset_bps`` floor (so we never cross the book if the
        # AS spread degenerates to ~0 at large τ).
        floor_offset = self.limit_offset_bps * 1e-4  # 50 bps = 0.005
        as_offset = mid - as_q.bid_price
        offset_used = max(as_offset, floor_offset)
        limit_price = max(1e-4, mid - offset_used)
        # Don't post inside the spread on the wrong side: we must be
        # ≤ the current bid (maker, no cross). If our limit is above
        # the existing bid, snap to bid - 1 tick (1 bp).
        existing_bid = ctx.pm_yes_bid if side == Side.YES_UP else ctx.pm_no_bid
        if existing_bid > 0 and limit_price > existing_bid:
            limit_price = max(1e-4, existing_bid - 1e-4)

        # 7. EV at the limit price (rather than at the ask, which is
        # what v1 uses). Maker fee = 0. This is the whole point of v2.
        ev_gross, ev_net = self._ev_with_limit(
            p_side=p_side, limit_price=limit_price
        )

        features = {
            "delta_pct": delta_pct,
            "sigma_per_sqrt_s": sigma,
            "tau_s": tau,
            "prob_up": prob_up,
            "p_side": p_side,
            "ask": ask,
            "mid": mid,
            "limit_price": limit_price,
            "as_half_spread": as_q.half_spread,
            "as_reservation_price": as_q.reservation_price,
            "as_bid_offset": as_q.bid_offset,
            "ev_gross": ev_gross,
            "ev_net": ev_net,
            "fee": 0.0,
            "n_sigma_ticks": len(spots),
            "execution_mode": "maker",
        }
        if cesta_dbg is not None:
            features.update({f"cesta_{k}": v for k, v in cesta_dbg.items()})

        # 8. EV gate (gross-of-fee since fee = 0 for maker).
        if ev_net < self.ev_threshold:
            # If we had an active quote, signal cancel by emitting SKIP
            # with reason="ev_decayed" — drivers/executors can trap this
            # (sim path: cancel-on-skip after stale_threshold).
            if ctx.market_slug in self._active:
                aq = self._active[ctx.market_slug]
                drop_bps = (aq.quoted_ev_net - ev_net) * 10000.0
                if drop_bps >= self.cancel_threshold_drop_bps:
                    self._active.pop(ctx.market_slug, None)
                    return Decision(
                        Action.SKIP,
                        reason="cancel_ev_decayed",
                        signal_features=features,
                    )
            return Decision(
                Action.SKIP,
                reason="ev_below_threshold",
                signal_features=features,
            )

        # 9. Re-quote logic: if we already have an active quote, only
        # emit a fresh ENTER when the new limit is materially different
        # OR side flipped, AND we respect cancel_min_interval_s.
        active = self._active.get(ctx.market_slug)
        if active is not None:
            same_side = active.side == side
            price_drift_bps = abs(active.limit_price - limit_price) * 10000.0
            since_last = ctx.ts - active.last_action_ts
            should_requote = (not same_side) or (
                price_drift_bps >= self.cancel_threshold_drop_bps
            )
            if should_requote and since_last < self.cancel_min_interval_s:
                # Rate-limit honouring: hold the previous quote one more tick.
                features["requote_throttled"] = True
                return Decision(
                    Action.SKIP,
                    reason="requote_throttled",
                    signal_features=features,
                )
            if not should_requote:
                # Quote is still valid — no action.
                features["quote_held"] = True
                return Decision(
                    Action.SKIP,
                    reason="quote_held",
                    signal_features=features,
                )

        # 10. Shadow gate (ADR-0011-style — promotion is operator action).
        shadow = bool(self.config.get("paper", {}).get("shadow", True))
        if shadow:
            return Decision(
                Action.SKIP, reason="shadow_mode", signal_features=features
            )

        # 11. Emit ENTER with order_type=GTC + limit_price. Update active
        # quote state so the next tick can compare.
        self._active[ctx.market_slug] = _ActiveQuote(
            side=side,
            limit_price=limit_price,
            quoted_ev_net=ev_net,
            quoted_ts=ctx.ts,
            last_action_ts=ctx.ts,
            cancel_count=(active.cancel_count + 1) if active else 0,
        )
        return Decision(
            action=Action.ENTER,
            side=side,
            order_type=OrderType.GTC,
            limit_price=limit_price,
            signal_features=features,
            reason=f"maker_quote ev_net={ev_net:+.4f} prob_up={prob_up:.3f}",
        )

    def on_trade_resolved(self, resolution: str, pnl: float, ts: float) -> None:
        """When the executor reports a fill (or the window closed), drop
        the active quote so the per-window state is clean. The driver
        calls this after settle, so it doubles as a per-market reset.
        """
        # No per-market info in the base signature; conservatively reset
        # everything per-window. v2 trades at most once per market (see
        # max_active_quotes_per_market = 1) so this is safe.
        self._active.clear()


__all__ = ["OracleLagV2"]
