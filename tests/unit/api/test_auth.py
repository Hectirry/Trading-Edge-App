"""Tests for API token auth (ADR 0009)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from trading.api.auth import require_token


def _make_request(
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Request:
    header_items: list[tuple[bytes, bytes]] = []
    if headers:
        for k, v in headers.items():
            header_items.append((k.lower().encode(), v.encode()))
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        header_items.append((b"cookie", cookie_header.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": header_items,
        "query_string": b"",
    }
    r = Request(scope)
    assert isinstance(r.headers, Headers)
    return r


class _StubSettings:
    def __init__(self, token: str) -> None:
        self.api_token = token


def test_require_token_accepts_matching_header() -> None:
    with patch("trading.api.auth.get_settings", return_value=_StubSettings("abc")):
        req = _make_request(headers={"X-TEA-Token": "abc"})
        require_token(req)


def test_require_token_accepts_matching_cookie() -> None:
    with patch("trading.api.auth.get_settings", return_value=_StubSettings("abc")):
        req = _make_request(cookies={"tea_token": "abc"})
        require_token(req)


def test_require_token_rejects_missing() -> None:
    with patch("trading.api.auth.get_settings", return_value=_StubSettings("abc")):
        req = _make_request()
        with pytest.raises(HTTPException) as exc:
            require_token(req)
        assert exc.value.status_code == 401


def test_require_token_rejects_wrong_value() -> None:
    with patch("trading.api.auth.get_settings", return_value=_StubSettings("abc")):
        req = _make_request(headers={"X-TEA-Token": "xyz"})
        with pytest.raises(HTTPException) as exc:
            require_token(req)
        assert exc.value.status_code == 401


def test_require_token_503_when_unconfigured() -> None:
    with patch("trading.api.auth.get_settings", return_value=_StubSettings("")):
        req = _make_request(headers={"X-TEA-Token": "anything"})
        with pytest.raises(HTTPException) as exc:
            require_token(req)
        assert exc.value.status_code == 503
