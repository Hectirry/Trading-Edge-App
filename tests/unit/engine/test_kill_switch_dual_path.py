"""Dual-path KILL_SWITCH (ADR 0009) — engine OR-reads both paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from trading.engine import node as engine_node


def _patched_paths(tmp_path: Path) -> tuple[Path, Path]:
    etc = tmp_path / "etc_killswitch"
    vartea = tmp_path / "var_tea_killswitch"
    return etc, vartea


def test_neither_present_returns_false(tmp_path: Path) -> None:
    etc, vartea = _patched_paths(tmp_path)
    with patch.object(engine_node, "KILL_SWITCH_PATHS", (str(etc), str(vartea))):
        assert engine_node.kill_switch_active() is False
        assert engine_node.kill_switch_which() is None


def test_etc_present_returns_true(tmp_path: Path) -> None:
    etc, vartea = _patched_paths(tmp_path)
    etc.write_text("x")
    with patch.object(engine_node, "KILL_SWITCH_PATHS", (str(etc), str(vartea))):
        assert engine_node.kill_switch_active() is True
        assert engine_node.kill_switch_which() == str(etc)


def test_vartea_present_returns_true(tmp_path: Path) -> None:
    etc, vartea = _patched_paths(tmp_path)
    vartea.write_text("x")
    with patch.object(engine_node, "KILL_SWITCH_PATHS", (str(etc), str(vartea))):
        assert engine_node.kill_switch_active() is True
        # which() returns the first path found in declaration order
        assert engine_node.kill_switch_which() == str(vartea)


def test_both_present_prefers_first_path(tmp_path: Path) -> None:
    etc, vartea = _patched_paths(tmp_path)
    etc.write_text("x")
    vartea.write_text("x")
    with patch.object(engine_node, "KILL_SWITCH_PATHS", (str(etc), str(vartea))):
        assert engine_node.kill_switch_active() is True
        assert engine_node.kill_switch_which() == str(etc)


def test_exec_client_also_honors_both_paths(tmp_path: Path) -> None:
    from trading.paper import exec_client

    etc, vartea = _patched_paths(tmp_path)
    vartea.write_text("x")
    with patch.object(exec_client, "KILL_SWITCH_PATHS", (str(etc), str(vartea))):
        assert exec_client.SimulatedExecutionClient.kill_switch_active() is True
