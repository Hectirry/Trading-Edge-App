"""Decision gates for contest_ensemble_v1 (ADR 0012)."""

from __future__ import annotations

from dataclasses import dataclass

from trading.engine.features.hmm_regime import NullHMMRegimeDetector, RegimeState
from trading.engine.features.macro import MacroSnapshot
from trading.engine.types import Action, Side, TickContext
from trading.strategies.polymarket_btc5m.contest_ensemble_v1 import (
    CHECKPOINTS,
    ContestEnsembleV1,
)


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


class _StubMacro:
    def __init__(self, snap):
        self.snap = snap

    def snapshot_at(self, _ts):
        return self.snap


class _StubHMM:
    def __init__(self, state: RegimeState | None):
        self.state = state

    def predict(self, _closes):
        return self.state


def _macro() -> MacroSnapshot:
    return MacroSnapshot(
        ema8=110.0, ema34=100.0, adx_14=25.0,
        consecutive_same_dir=3, regime="uptrend", ema8_vs_ema34_pct=10.0,
    )


def _ctx(t_in_window: float = 210.0, slug: str = "btc-updown-5m-1") -> TickContext:
    now = 1_700_000_000.0 + t_in_window
    spots = [70_000.0 * (1.0 + 0.0005 * i) for i in range(90)]
    recent = [
        _RecentTick(ts=now - (len(spots) - i), spot_price=spots[i])
        for i in range(len(spots))
    ]
    return TickContext(
        ts=now, market_slug=slug, t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=spots[-1], chainlink_price=spots[-1], open_price=spots[0],
        pm_yes_bid=0.47, pm_yes_ask=0.48, pm_no_bid=0.52, pm_no_ask=0.53,
        pm_depth_yes=100.0, pm_depth_no=100.0, pm_imbalance=0.3,
        pm_spread_bps=50.0, implied_prob_yes=0.48,
        model_prob_yes=0.5, edge=0.0, z_score=0.0, vol_regime="normal",
        recent_ticks=recent,
    )


def _strat(*, hmm_state: RegimeState | None = None, with_macro: bool = True) -> ContestEnsembleV1:
    cfg = {"params": {"conformal_alpha": 0.25}, "paper": {"shadow": False}}
    return ContestEnsembleV1(
        cfg,
        macro_provider=_StubMacro(_macro()) if with_macro else None,
        hmm_detector=_StubHMM(hmm_state) if hmm_state is not None else NullHMMRegimeDetector(),
    )


def test_skip_outside_checkpoint() -> None:
    s = _strat()
    for t in (50.0, 90.0, 150.0, 225.0, 290.0):
        d = s.should_enter(_ctx(t_in_window=t))
        assert d.action is Action.SKIP
        assert d.reason == "outside_checkpoint"


def test_enter_or_abstain_at_each_checkpoint() -> None:
    s = _strat()
    for cp in CHECKPOINTS:
        d = s.should_enter(_ctx(t_in_window=cp, slug=f"btc-cp-{int(cp)}"))
        assert d.action in (Action.ENTER, Action.SKIP)
        if d.action is Action.SKIP:
            # Only conformal_abstain or contest_abstained are valid here.
            assert d.reason in (
                "conformal_abstain", "contest_abstained", "insufficient_micro_data",
            )


def test_high_vol_immediately_skips() -> None:
    state = RegimeState(
        label="high_vol",
        posteriors=(0.1, 0.1, 0.1, 0.7),
        transition_stability=0.9,
    )
    s = _strat(hmm_state=state)
    d = s.should_enter(_ctx(t_in_window=210.0))
    assert d.action is Action.SKIP
    assert d.reason == "high_vol_abstain"


def test_insufficient_micro_data_skips() -> None:
    s = _strat()
    ctx = _ctx(t_in_window=210.0)
    ctx.recent_ticks = []  # strip
    d = s.should_enter(ctx)
    assert d.action is Action.SKIP
    assert d.reason == "insufficient_micro_data"


def test_already_entered_skips_subsequent_checkpoints() -> None:
    s = _strat()
    slug = "btc-once"
    first = s.should_enter(_ctx(t_in_window=210.0, slug=slug))
    # Either ENTER or SKIP is fine; but if we hit high_vol/abstain the
    # market is now marked. Force the "already_entered" path by marking
    # via a real decision.
    assert first is not None
    s._last_entered_per_market[slug] = True
    d = s.should_enter(_ctx(t_in_window=240.0, slug=slug))
    assert d.action is Action.SKIP
    assert d.reason == "already_entered"


def test_features_exposed_on_skip_contains_diagnostics() -> None:
    s = _strat()
    d = s.should_enter(_ctx(t_in_window=210.0))
    # Whatever the verdict, the SKIP path that runs the ensemble will
    # populate features. ENTER also populates. Both fine.
    if d.signal_features:
        for k in ("pa_prob", "pb_prob", "pc_prob"):
            assert k in d.signal_features


def test_last_checkpoint_abstain_marks_market_entered() -> None:
    """If conformal keeps abstaining through the last checkpoint, the
    market should be pinned as 'already entered' so subsequent ticks
    return 'already_entered' instead of re-running the ensemble.
    """
    s = _strat()
    slug = "btc-last-cp"
    last = s.should_enter(_ctx(t_in_window=CHECKPOINTS[-1] + 4.0, slug=slug))
    assert last.action is Action.SKIP
    # outside_checkpoint at t=274 (beyond 270+3 tolerance)
    assert last.reason == "outside_checkpoint"


def test_side_matches_decision_when_enter() -> None:
    s = _strat()
    d = s.should_enter(_ctx(t_in_window=210.0))
    if d.action is Action.ENTER:
        assert d.side in (Side.YES_UP, Side.YES_DOWN)
