"""Decision gates for contest_avengers_v1 (ADR 0012)."""

from __future__ import annotations

from dataclasses import dataclass

from trading.engine.features.hmm_regime import NullHMMRegimeDetector, RegimeState
from trading.engine.features.liquidation_gravity import LiqCluster
from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.contest_avengers_v1 import (
    CHECKPOINTS,
    ContestAvengersV1,
)


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


class _StubHMM:
    def __init__(self, state: RegimeState | None):
        self.state = state

    def predict(self, _closes):
        return self.state


@dataclass
class _StubChainlink:
    row: dict | None

    def snapshot(self):
        return self.row


@dataclass
class _StubLiq:
    rows: list[LiqCluster]

    def snapshot(self):
        return list(self.rows)


def _ctx(t_in_window: float = 210.0, slug: str = "btc-av-1") -> TickContext:
    now = 1_700_000_000.0 + t_in_window
    spots = [70_000.0 + 0.1 * i for i in range(90)]
    recent = [_RecentTick(ts=now - (len(spots) - i), spot_price=s) for i, s in enumerate(spots)]
    return TickContext(
        ts=now,
        market_slug=slug,
        t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=70_100.0,
        chainlink_price=70_100.0,
        open_price=70_000.0,
        pm_yes_bid=0.47,
        pm_yes_ask=0.48,
        pm_no_bid=0.52,
        pm_no_ask=0.53,
        pm_depth_yes=100.0,
        pm_depth_no=100.0,
        pm_imbalance=0.5,
        pm_spread_bps=50.0,
        implied_prob_yes=0.48,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=0.0,
        vol_regime="normal",
        recent_ticks=recent,
    )


def _strat(
    *,
    chainlink: _StubChainlink | None = None,
    liq: _StubLiq | None = None,
    hmm_state: RegimeState | None = None,
    confidence_threshold: float = 0.75,
) -> ContestAvengersV1:
    cfg = {
        "params": {"confidence_threshold": confidence_threshold},
        "paper": {"shadow": False},
    }
    return ContestAvengersV1(
        cfg,
        hmm_detector=_StubHMM(hmm_state) if hmm_state is not None else NullHMMRegimeDetector(),
        chainlink_provider=chainlink,
        liq_provider=liq,
    )


def test_outside_checkpoint_skips() -> None:
    s = _strat()
    d = s.should_enter(_ctx(t_in_window=100.0))
    assert d.action is Action.SKIP
    assert d.reason == "outside_checkpoint"


def test_no_chainlink_no_liq_skips_confidence() -> None:
    s = _strat()
    d = s.should_enter(_ctx(t_in_window=210.0))
    assert d.action is Action.SKIP
    # Without chainlink direction, strategy waits for next checkpoint.
    assert d.reason in (
        "awaiting_directional_signal",
        "confidence_below_threshold",
    )


def test_high_vol_hard_skip() -> None:
    state = RegimeState(
        label="high_vol",
        posteriors=(0.1, 0.1, 0.1, 0.7),
        transition_stability=0.8,
    )
    s = _strat(hmm_state=state)
    d = s.should_enter(_ctx(t_in_window=210.0))
    assert d.action is Action.SKIP
    assert d.reason == "high_vol_skip"


def test_strong_chainlink_lag_triggers_enter() -> None:
    """Large age + delta → lag_score ≈ 1 → confidence ≥ 0.5.

    Combined with a matching liquidation cluster it crosses 0.75 even
    without a saturating HMM bonus.
    """
    chainlink = _StubChainlink(
        row={
            "answer": 69_500.0,  # binance spot 70_100 → delta ≈ +86 bps
            "updated_at_ts": 0.0,
            "age_s": 20.0,
            "source": "eac_polygon",
        }
    )
    # Cluster above spot, large, close → up-side gravity saturates.
    liq = _StubLiq(
        rows=[
            LiqCluster(ts=0.0, side="short", price=70_100.0 * 1.0005, size_usd=500_000.0),
        ]
    )
    s = _strat(chainlink=chainlink, liq=liq, confidence_threshold=0.50)
    d = s.should_enter(_ctx(t_in_window=240.0))
    assert d.action is Action.ENTER
    assert d.side is Side.YES_UP
    assert d.signal_features["confidence_mag"] >= 0.5


def test_below_threshold_final_checkpoint_skips() -> None:
    # Modest lag → score ~ 0.3; confidence ≈ 0.15 < 0.75. At last cp → skip.
    chainlink = _StubChainlink(
        row={
            "answer": 70_050.0,
            "updated_at_ts": 0.0,
            "age_s": 8.0,
            "source": "eac_polygon",
        }
    )
    s = _strat(chainlink=chainlink, confidence_threshold=0.75)
    d = s.should_enter(_ctx(t_in_window=CHECKPOINTS[-1]))
    assert d.action is Action.SKIP
    # Could be below_threshold_final if direction non-zero.
    assert d.reason in (
        "confidence_below_threshold_final",
        "confidence_below_threshold",
    )


def test_graceful_degradation_caps_confidence_at_0_85() -> None:
    # Huge lag (saturated) but NO liquidation provider set → capped.
    chainlink = _StubChainlink(
        row={
            "answer": 65_000.0,
            "updated_at_ts": 0.0,
            "age_s": 30.0,
            "source": "eac_polygon",
        }
    )
    s = _strat(chainlink=chainlink, liq=None)
    d = s.should_enter(_ctx(t_in_window=210.0))
    assert d.signal_features is not None
    assert d.signal_features.get("degraded") is True
    assert d.signal_features["confidence_mag"] <= 0.85


def test_already_entered_blocks_subsequent_checkpoints() -> None:
    s = _strat()
    s._last_entered_per_market["pin"] = True
    d = s.should_enter(_ctx(t_in_window=270.0, slug="pin"))
    assert d.action is Action.SKIP
    assert d.reason == "already_entered"
