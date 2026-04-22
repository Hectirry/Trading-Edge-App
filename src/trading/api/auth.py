"""Token auth — `X-TEA-Token` header or `tea_token` cookie. No scopes.

Token lives in TEA_API_TOKEN env. Missing/mismatching → 401.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from trading.common.config import get_settings


def require_token(request: Request) -> None:
    settings = get_settings()
    expected = settings.api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="api_token not configured on server",
        )
    presented = request.headers.get("X-TEA-Token") or request.cookies.get("tea_token")
    if not presented or presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-TEA-Token",
        )
