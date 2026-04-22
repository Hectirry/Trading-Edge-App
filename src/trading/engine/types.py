from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Action(str, Enum):
    ENTER = "ENTER"
    SKIP = "SKIP"


class Side(str, Enum):
    YES_UP = "YES_UP"
    YES_DOWN = "YES_DOWN"
    NONE = "NONE"


class OrderType(str, Enum):
    MARKET = "MARKET"  # FAK: immediate fill against resting liquidity
    GTC = "GTC"  # Good-Till-Cancel, rests in book until filled or TTL


@dataclass
class TickContext:
    """Snapshot of state at a single tick, input to the strategy."""

    ts: float
    market_slug: str
    t_in_window: float
    window_close_ts: float
    spot_price: float
    chainlink_price: float | None
    open_price: float
    pm_yes_bid: float
    pm_yes_ask: float
    pm_no_bid: float
    pm_no_ask: float
    pm_depth_yes: float
    pm_depth_no: float
    pm_imbalance: float
    pm_spread_bps: float
    implied_prob_yes: float
    model_prob_yes: float
    edge: float
    z_score: float
    vol_regime: str
    recent_ticks: list = field(default_factory=list)
    # Derived at tick-reconstruction time, populated by IndicatorStack.update().
    t_to_close: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi_14: float = 50.0
    vol_realized: float = 0.0
    vol_ewma: float = 0.0
    # Derived from spot_price vs open_price (bps). Populated by loaders /
    # tick_recorder so strategies don't recompute it.
    delta_bps: float = 0.0

    @property
    def depth_total_usd(self) -> float:
        return self.pm_depth_yes + self.pm_depth_no


@dataclass
class Decision:
    action: Action
    side: Side = Side.NONE
    reason: str = ""
    signal_features: dict = field(default_factory=dict)
    signal_breakdown: dict = field(default_factory=dict)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    ttl_seconds: int | None = None
    horizon_s: int | None = None
    size_usd: float | None = None


@dataclass
class Order:
    order_id: str
    strategy_id: str
    instrument_id: str
    side: str
    order_type: str
    qty: Decimal
    price: Decimal | None
    status: str
    ts_submit: float
    ts_last_update: float
    mode: str
    backtest_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Fill:
    fill_id: str
    order_id: str
    ts: float
    price: Decimal
    qty: Decimal
    fee: Decimal
    mode: str
    backtest_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class PositionSnapshot:
    ts: float
    strategy_id: str
    instrument_id: str
    qty: Decimal
    avg_price: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    mode: str
    backtest_id: str | None = None
