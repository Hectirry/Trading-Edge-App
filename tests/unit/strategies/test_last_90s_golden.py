"""Golden-own-trace parity for v1 + v2 (ADR 0011).

We feed a deterministic synthetic scenario — a mock market with a
rising BTC track over the last 90 s and a mild uptrend macro
snapshot — into each strategy across the entry window, serialise each
Decision as a compact dict, and diff against a JSON fixture. Any
behavioural change must update the fixture in the same commit with a
clear reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from trading.engine.features.macro import MacroSnapshot
from trading.engine.types import TickContext
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v1 import (
    Last90sForecasterV1,
)
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import (
    Last90sForecasterV2,
)

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden"


@dataclass
class _RecentTick:
    ts: float
    spot_price: float


class _StubMacro:
    def __init__(self, snap):
        self.snap = snap

    def snapshot_at(self, _ts):
        return self.snap


class _StubModel:
    def predict_proba(self, _x):
        return 0.62  # deterministic


def _macro() -> MacroSnapshot:
    return MacroSnapshot(
        ema8=110.0,
        ema34=100.0,
        adx_14=25.0,
        consecutive_same_dir=3,
        regime="uptrend",
        ema8_vs_ema34_pct=10.0,
    )


def _ctx_at(t_in_window: float, implied: float) -> TickContext:
    now = 1_700_000_000.0 + t_in_window
    spots = [70_000.0 * (1.0 + 0.01 * i / 89.0) for i in range(90)]
    recent = [
        _RecentTick(ts=now - (len(spots) - i), spot_price=spots[i]) for i in range(len(spots))
    ]
    return TickContext(
        ts=now,
        market_slug="btc-updown-5m-golden",
        t_in_window=t_in_window,
        window_close_ts=now + (300 - t_in_window),
        spot_price=spots[-1],
        chainlink_price=spots[-1],
        open_price=spots[0],
        pm_yes_bid=0.47,
        pm_yes_ask=0.48,
        pm_no_bid=0.52,
        pm_no_ask=0.53,
        pm_depth_yes=100.0,
        pm_depth_no=100.0,
        pm_imbalance=0.0,
        pm_spread_bps=50.0,
        implied_prob_yes=implied,
        model_prob_yes=0.5,
        edge=0.0,
        z_score=0.0,
        vol_regime="normal",
        recent_ticks=recent,
    )


def _decision_to_dict(d) -> dict:
    def _round(v):
        return round(v, 6) if isinstance(v, float) else v

    return {
        "action": d.action.value,
        "side": d.side.value,
        "reason": d.reason,
        "signal_features": {k: _round(v) for k, v in d.signal_features.items()},
    }


def _scenario() -> list[tuple[float, float]]:
    # (t_in_window, implied_prob_yes) sweep — spans the entry window
    # plus a point before and after.
    out: list[tuple[float, float]] = []
    for t in (190.0, 205.0, 210.0, 215.0, 220.0):
        for p in (0.30, 0.48, 0.70):
            out.append((t, p))
    return out


def _run_v1() -> list[dict]:
    cfg = {
        "params": {
            "entry_window_start_s": 205,
            "entry_window_end_s": 215,
            "momentum_divisor_bps": 40.0,
            "edge_threshold": 0.04,
            "spread_max_bps": 150.0,
            "adx_threshold": 20.0,
            "consecutive_min": 2,
        }
    }
    s = Last90sForecasterV1(cfg, macro_provider=_StubMacro(_macro()))
    return [
        {"t_in_window": t, "implied": p, **_decision_to_dict(s.should_enter(_ctx_at(t, p)))}
        for (t, p) in _scenario()
    ]


def _run_v2() -> list[dict]:
    cfg = {
        "params": {
            "entry_window_start_s": 205,
            "entry_window_end_s": 215,
            "edge_threshold": 0.04,
            "spread_max_bps": 150.0,
            "adx_threshold": 20.0,
            "consecutive_min": 2,
        },
        "paper": {"shadow": False},
    }
    s = Last90sForecasterV2(cfg, macro_provider=_StubMacro(_macro()), model=_StubModel())
    return [
        {"t_in_window": t, "implied": p, **_decision_to_dict(s.should_enter(_ctx_at(t, p)))}
        for (t, p) in _scenario()
    ]


def _load_or_create_golden(path: Path, produced: list[dict]) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(produced, indent=2))
    return produced


def test_v1_golden_parity() -> None:
    path = GOLDEN_DIR / "last_90s_forecaster_v1.json"
    got = _run_v1()
    want = _load_or_create_golden(path, got)
    assert got == want, (
        "v1 trace drifted vs fixture. Review the diff and, if the change "
        "is intentional, rebuild with `rm tests/fixtures/golden/"
        "last_90s_forecaster_v1.json && pytest`."
    )


def test_v2_golden_parity() -> None:
    path = GOLDEN_DIR / "last_90s_forecaster_v2.json"
    got = _run_v2()
    want = _load_or_create_golden(path, got)
    assert got == want, (
        "v2 trace drifted vs fixture. Review the diff and, if intentional, "
        "rebuild with `rm tests/fixtures/golden/"
        "last_90s_forecaster_v2.json && pytest`."
    )
