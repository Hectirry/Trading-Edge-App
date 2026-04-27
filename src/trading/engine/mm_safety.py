"""MMSafetyGuard — non-bypassable safety gates for market-making strategies.

Distinct from `RiskManager` (which is bypassable in backtest via
`[risk].bypass_in_backtest=true` for parity with polybot-agent). This
guard enforces invariants that protect against simulator bugs and
strategy degeneration; those invariants must hold in backtest, paper,
and live alike.

Gates enforced
--------------
- `inventory_cap_usdc`: `|q × p_market| ≤ cap`. Block PostQuote that
  would extend inventory past the cap.
- `cancel_fill_ratio_max`: rolling 5-min ratio of cancels / fills per
  market. On 1st breach in a 60-min window: alert. On 2nd breach within
  the window: kill quotes for that market for `resume_minutes` minutes.
- `taker_fee_canary`: rolling 7-day ratio of taker_fee_paid /
  total_pnl_gross. If it breaches `canary_pct`, the strategy has
  degenerated and the guard demands a paper-pause.
- `tau_terminal_s`: do not allow PostQuote in the last
  `tau_terminal_s` seconds of the window. Forced taker outflow only.

The guard is stateless across strategies — one instance per strategy.
State is in-memory; persistence across restarts is owed to the strategy
class via on_start hooks (Step 0 v2 follow-up).

Note on aggressive paper mode (mm_rebate_v1 V1)
-----------------------------------------------
The user accepts surfacing pathology over auto-killing the strategy.
For paper mode, `auto_kill_on_breach` is intentionally `False`: the guard
emits alerts but does not stop the strategy. Live mode flips the flag.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class MMSafetyParams:
    inventory_cap_usdc: float = 50.0           # paper-aggressive default
    cancel_fill_ratio_max: float = 30.0        # paper-aggressive: 30:1
    cancel_fill_window_minutes: int = 5
    cancel_fill_kill_threshold: int = 2        # 2 disparos = kill el mercado
    cancel_fill_kill_window_min: int = 60
    cancel_fill_resume_min: int = 15
    taker_fee_canary_pct: float = 0.05
    taker_fee_canary_window_days: int = 7
    tau_terminal_s: float = 30.0               # paper-aggressive: 30s (vs 60s conservador)
    auto_kill_on_breach: bool = False          # paper: alerts only


@dataclass
class _MarketMMState:
    cancels: deque = field(default_factory=deque)         # (ts, count)
    fills: deque = field(default_factory=deque)
    breach_events: deque = field(default_factory=deque)   # ts of cancel/fill ratio breaches
    killed_until: datetime | None = None
    last_alert_ts: datetime | None = None


class MMSafetyGuard:
    """Per-strategy MM safety enforcer.

    `block_post_quote(...)` / `record_post`, `record_cancel`, `record_fill`
    are all the strategy / driver should call.
    """

    def __init__(self, strategy_id: str, params: MMSafetyParams | None = None) -> None:
        self.strategy_id = strategy_id
        self.params = params or MMSafetyParams()
        self._market_state: dict[str, _MarketMMState] = {}
        # Inventory tracked per market in shares (signed). Cap evaluated
        # against `|q × p_market|`.
        self._inventory_shares: dict[str, float] = {}
        # Rolling 7d for canary
        self._taker_fee_paid_usdc: deque = deque()  # (ts, usdc)
        self._pnl_gross_usdc: deque = deque()       # (ts, usdc)
        # Counter of alert events for tests / visibility
        self.alerts: list[dict] = []

    # ─── inventory cap ────────────────────────────────────────────────

    def block_post_quote(self, market_slug: str, side_sign: int, qty_shares: float, p_market: float) -> tuple[bool, str]:
        """Decide whether to block a PostQuote that would extend inventory.

        Returns (blocked, reason). `side_sign`: +1 if posting BID-side
        on YES (would buy YES → inventory goes more long), -1 if posting
        ASK-side on YES (would sell YES → inventory goes more short).
        """
        current_q = self._inventory_shares.get(market_slug, 0.0)
        # Project worst-case inventory if this quote fills entirely
        projected_q = current_q + side_sign * qty_shares
        projected_usdc = abs(projected_q * p_market)
        if projected_usdc > self.params.inventory_cap_usdc:
            return True, (
                f"inventory_cap_usdc breach: projected ${projected_usdc:.2f} "
                f"> cap ${self.params.inventory_cap_usdc:.2f}"
            )
        return False, "ok"

    def block_post_quote_terminal(self, t_in_window_s: float, window_seconds: int) -> tuple[bool, str]:
        """Block PostQuote in the last `tau_terminal_s` seconds of the window."""
        if t_in_window_s > (window_seconds - self.params.tau_terminal_s):
            return True, f"terminal window: t={t_in_window_s:.0f} > T - {self.params.tau_terminal_s}"
        return False, "ok"

    def record_inventory_after_fill(self, market_slug: str, side_sign: int, qty_shares: float) -> None:
        cur = self._inventory_shares.get(market_slug, 0.0)
        self._inventory_shares[market_slug] = cur + side_sign * qty_shares

    def inventory_shares(self, market_slug: str) -> float:
        return self._inventory_shares.get(market_slug, 0.0)

    # ─── cancel/fill ratio ────────────────────────────────────────────

    def record_cancel(self, market_slug: str, now: datetime | None = None) -> None:
        now = now or datetime.now(tz=UTC)
        st = self._market_state.setdefault(market_slug, _MarketMMState())
        st.cancels.append(now)
        self._gc_window(st, now)
        self._maybe_breach(market_slug, st, now)

    def record_fill(self, market_slug: str, now: datetime | None = None) -> None:
        now = now or datetime.now(tz=UTC)
        st = self._market_state.setdefault(market_slug, _MarketMMState())
        st.fills.append(now)
        self._gc_window(st, now)

    def is_market_killed(self, market_slug: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        st = self._market_state.get(market_slug)
        if st is None or st.killed_until is None:
            return False
        if now >= st.killed_until:
            st.killed_until = None
            return False
        return True

    def _gc_window(self, st: _MarketMMState, now: datetime) -> None:
        cutoff = now - timedelta(minutes=self.params.cancel_fill_window_minutes)
        while st.cancels and st.cancels[0] < cutoff:
            st.cancels.popleft()
        while st.fills and st.fills[0] < cutoff:
            st.fills.popleft()
        kill_cutoff = now - timedelta(minutes=self.params.cancel_fill_kill_window_min)
        while st.breach_events and st.breach_events[0] < kill_cutoff:
            st.breach_events.popleft()

    def _maybe_breach(self, market_slug: str, st: _MarketMMState, now: datetime) -> None:
        n_fills = max(1, len(st.fills))  # avoid div0
        ratio = len(st.cancels) / n_fills
        if ratio <= self.params.cancel_fill_ratio_max:
            return
        # Dedupe: a breach is one event per minute, not one per cancel.
        # Without this, a 15-cancel burst inflates breach_events 15× and
        # spuriously trips the kill threshold.
        if st.breach_events and (now - st.breach_events[-1]) < timedelta(minutes=1):
            return
        st.breach_events.append(now)
        n_breaches = len(st.breach_events)
        self.alerts.append(
            {
                "kind": "cancel_fill_breach",
                "ts": now.isoformat(),
                "market_slug": market_slug,
                "ratio": ratio,
                "n_breaches_in_window": n_breaches,
            }
        )
        # 2nd breach within kill window → kill that market for resume_min
        if n_breaches >= self.params.cancel_fill_kill_threshold and self.params.auto_kill_on_breach:
            st.killed_until = now + timedelta(minutes=self.params.cancel_fill_resume_min)

    # ─── taker fee canary ─────────────────────────────────────────────

    def record_taker_fee_paid(self, usdc: float, now: datetime | None = None) -> None:
        now = now or datetime.now(tz=UTC)
        self._taker_fee_paid_usdc.append((now, usdc))
        self._gc_canary(now)

    def record_pnl_gross(self, usdc: float, now: datetime | None = None) -> None:
        now = now or datetime.now(tz=UTC)
        self._pnl_gross_usdc.append((now, usdc))
        self._gc_canary(now)

    def _gc_canary(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.params.taker_fee_canary_window_days)
        while self._taker_fee_paid_usdc and self._taker_fee_paid_usdc[0][0] < cutoff:
            self._taker_fee_paid_usdc.popleft()
        while self._pnl_gross_usdc and self._pnl_gross_usdc[0][0] < cutoff:
            self._pnl_gross_usdc.popleft()

    def canary_ratio(self) -> float:
        fee = sum(v for _, v in self._taker_fee_paid_usdc)
        gross = sum(v for _, v in self._pnl_gross_usdc)
        if gross <= 0:
            return 0.0
        return fee / gross

    def canary_breached(self) -> bool:
        return self.canary_ratio() > self.params.taker_fee_canary_pct
