"""Context loader sanitization + budget trimming (ADR 0010)."""

from __future__ import annotations

import pytest

from trading.llm.context_loader import (
    ALLOWED_TYPES,
    BYTE_BUDGET,
    ContextRef,
    LoadedContext,
    _budget_trim,
    _sanitize,
)


def test_context_ref_parse_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        ContextRef.parse({"type": "system_command", "id": "x"})


def test_context_ref_parse_accepts_all_known_types() -> None:
    for t in ALLOWED_TYPES:
        ref = ContextRef.parse({"type": t, "id": "x"})
        assert ref.type == t


def test_context_ref_parse_requires_id() -> None:
    with pytest.raises(ValueError):
        ContextRef.parse({"type": "backtest", "id": ""})


def test_sanitize_strips_control_chars() -> None:
    s = "hello\x1b[31mRED\x00bye\x07"
    out = _sanitize(s)
    assert "\x1b" not in out
    assert "\x00" not in out
    assert "\x07" not in out
    assert "\n" not in out or "\n" in s  # newlines preserved when present


def test_sanitize_preserves_newline_and_tab() -> None:
    assert _sanitize("a\nb\tc") == "a\nb\tc"


def test_budget_trim_below_cap_is_passthrough() -> None:
    body, trunc = _budget_trim("abc", 100)
    assert body == "abc"
    assert trunc == 0


def test_budget_trim_above_cap_head_cuts_with_count() -> None:
    body = "x" * 120
    trimmed, trunc = _budget_trim(body, 100)
    assert len(trimmed) == 100
    assert trunc == 20


def test_loaded_context_render_wraps_in_fence_with_escaping() -> None:
    ref = ContextRef(type="adr", id='weird"id<')
    lc = LoadedContext(ref=ref, body="line1\nline2", truncated_bytes=5)
    out = lc.render()
    assert out.startswith('<context type="adr" id="weird&quot;id&lt;">')
    assert out.endswith("</context>")
    assert "line1\nline2" in out
    assert "[truncated 5 bytes]" in out


def test_budgets_cover_all_allowed_types() -> None:
    assert set(BYTE_BUDGET.keys()) == set(ALLOWED_TYPES)
