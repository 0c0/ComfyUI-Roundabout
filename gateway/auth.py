"""简易鉴权：Bearer Token（兼容 api-key / x-api-key 头）。

API_KEYS 为空时自动放行，方便本机联调；生产务必配置。
"""

from __future__ import annotations

import hmac

from aiohttp import web

from .config import settings
from .errors import AuthError

_PUBLIC_PATHS = {"/health", "/healthz", "/", "/docs", "/openapi.json", "/redoc"}


def _extract_token(request: web.Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return auth.strip()
    for header in ("api-key", "x-api-key"):
        v = request.headers.get(header)
        if v:
            return v.strip()
    return None


def verify(request: web.Request) -> None:
    """校验请求；失败抛 AuthError。"""
    if not settings.auth_enabled:
        return
    if request.path in _PUBLIC_PATHS or request.path.startswith("/static"):
        return

    token = _extract_token(request)
    if not token:
        raise AuthError("Missing API key. Provide it via `Authorization: Bearer <key>`.", code="missing_api_key")

    # 常量时间比较，避免时序侧信道
    if not any(hmac.compare_digest(token, key) for key in settings.api_keys):
        raise AuthError()
