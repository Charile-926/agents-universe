"""会话级认证路由：当前用户与登出。

账号注册与登录见 ``auth_local.py``（账号 + 密码，用户信息加密落库）。
平台不再提供 OAuth/SSO 登录入口：每个用户都必须用账号密码登录。
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.errors import api_error
from api.services.preferences import get_or_create_preferences, serialize_preferences
from api.services.redis_client import delete_session, get_redis

router = APIRouter(tags=["auth"])

_log = logging.getLogger("agents_universe.auth")


def _check_logout_origin(request: Request, settings) -> None:
    """拒绝跨站登出请求（GET 端点的 CSRF 防护）。

    允许的来源：前端站点、API 自身 host（前端由 API 同域提供，可能通过任意
    主机名/IP 访问）、以及本地回环地址。没有 Referer 时用 Fetch Metadata 的
    ``Sec-Fetch-Site`` 兜底；两者都缺失视为直接操作（curl / API 客户端）。
    """
    referer = request.headers.get("referer") or request.headers.get("origin")
    if not referer:
        if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
            _log.warning("Cross-site logout attempt blocked: no referer, sec-fetch-site=cross-site")
            raise api_error(403, "AUTH_CROSS_SITE_LOGOUT", "Cross-site logout blocked")
        return
    try:
        ref_host = urlsplit(referer).hostname or ""
    except ValueError:
        return

    allowed: set[str] = set()
    try:
        parsed = urlsplit(settings.web_base_url)
        if parsed.hostname:
            allowed.add(parsed.hostname.lower())
    except ValueError:
        pass
    api_host = (request.headers.get("host") or "").strip()
    if api_host:
        try:
            host = urlsplit(f"//{api_host}").hostname
            if host:
                allowed.add(host.lower())
        except ValueError:
            pass
    allowed.update({"localhost", "127.0.0.1", "::1"})
    if ref_host.lower() not in allowed:
        _log.warning("Cross-site logout attempt blocked: referer=%s", referer[:120])
        raise api_error(403, "AUTH_CROSS_SITE_LOGOUT", "Cross-site logout blocked")


@router.get("/auth/logout")
async def logout(request: Request, redis: Redis = Depends(get_redis)):
    """删除 Redis 会话并清掉 Cookie。"""
    settings = get_settings()
    _check_logout_origin(request, settings)
    session_id = request.cookies.get(settings.auth_cookie_name)
    if session_id:
        await delete_session(redis, session_id)
    response = RedirectResponse(url=settings.web_base_url)
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    return response


@router.get("/api/me")
async def me(
    current_user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = await get_or_create_preferences(db, current_user.user_id)
    return {
        "user_id": current_user.user_id,
        "display_name": current_user.display_name,
        "preferences": serialize_preferences(prefs),
    }
