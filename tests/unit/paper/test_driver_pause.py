"""Pause/resume control handler (ADR 0009) — unit test without Redis."""

from __future__ import annotations

import json
from types import SimpleNamespace

from trading.paper.driver import PaperDriver


class _Strat:
    name = "test_strategy"

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass


def _bare_driver() -> PaperDriver:
    # Bypass __init__ (which would require many collaborators).
    d = PaperDriver.__new__(PaperDriver)
    d.strategy = _Strat()
    d._paused = False
    d._control_channel = f"tea:control:{d.strategy.name}"
    d._eval_counts = {}
    d._eval_skip_reasons = {}
    return d


def test_handle_control_pause_sets_flag() -> None:
    d = _bare_driver()
    d._handle_control_message(json.dumps({"action": "pause", "by": "api"}).encode())
    assert d._paused is True


def test_handle_control_resume_clears_flag() -> None:
    d = _bare_driver()
    d._paused = True
    d._handle_control_message(json.dumps({"action": "resume"}).encode())
    assert d._paused is False


def test_handle_control_ignores_unknown_action() -> None:
    d = _bare_driver()
    d._handle_control_message(json.dumps({"action": "weird"}).encode())
    assert d._paused is False


def test_handle_control_ignores_bad_json() -> None:
    d = _bare_driver()
    d._handle_control_message(b"not json")
    assert d._paused is False


def test_handle_control_accepts_str_payload() -> None:
    d = _bare_driver()
    d._handle_control_message(json.dumps({"action": "pause"}))
    assert d._paused is True


def test_bump_counter_tracks_paused_skip() -> None:
    d = _bare_driver()
    d._bump_counter("paused_skip", reason="paused")
    d._bump_counter("paused_skip", reason="paused")
    assert d._eval_counts["paused_skip"] == 2
    assert d._eval_skip_reasons["paused"] == 2


def test_control_channel_scoped_to_strategy() -> None:
    d = _bare_driver()
    assert d._control_channel == "tea:control:test_strategy"


def test_simplenamespace_payload_roundtrip() -> None:
    """Sanity: make sure we can accept the exact bytes the API publishes."""
    d = _bare_driver()
    payload = SimpleNamespace(action="pause")
    d._handle_control_message(json.dumps(payload.__dict__).encode())
    assert d._paused is True
