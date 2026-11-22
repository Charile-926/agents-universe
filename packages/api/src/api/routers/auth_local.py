"""本地账号认证：注册 / 登录 / 登出 / 当前用户。

平台只接受"账号 + 密码"登录。用户信息在落库前做两层处理：

- 密码：PBKDF2-HMAC-SHA256 单向哈希（每用户独立盐），不可逆；
- 用户名与显示名：AES 加密后存储（见 ``services.user_crypto``），
  另存一份 HMAC 摘要用于唯一约束与登录查找，数据库中不出现明文。

会话沿用 Redis + HttpOnly Cookie，REST 与 WebSocket 共用同一会话。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.errors import api_error
from api.models.user import UserAccount
from api.services.redis_client import delete_session, get_redis, save_session
from api.services.user_crypto import (
    decrypt_text,
    encrypt_text,
    hash_password,
    username_digest,
    verify_password,
)

router = APIRouter(tags=["auth"])
_log = logging.getLogger("agents_universe.auth")


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=6, max_length=200)
    display_name: str | None = Field(default=None, max_length=255)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def _set_session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=session_id,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


async def _open_session(
    redis: Redis, user_id: str, display_name: str, response: Response
) -> dict:
    session_id = str(uuid.uuid4())
    await save_session(
        redis,
        session_id,
        {"user_id": user_id, "display_name": display_name},
        ttl=get_settings().session_ttl,
    )
    _set_session_cookie(response, session_id)
    return {"user_id": user_id, "display_name": display_name}


@router.post("/api/auth/register")
async def register(
    body: RegisterBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    username = body.username.strip()
    if not username:
        raise api_error(400, "AUTH_USERNAME_REQUIRED", "用户名不能为空")

    digest = username_digest(username)
    existing = await db.execute(
        select(UserAccount.user_id).where(UserAccount.username_hash == digest)
    )
    if existing.scalar_one_or_none():
        raise api_error(409, "AUTH_USERNAME_TAKEN", "该用户名已被注册")

    display_name = (body.display_name or username).strip()
    account = UserAccount(
        username_hash=digest,
        username_enc=encrypt_text(username),
        display_name_enc=encrypt_text(display_name),
        password_hash=hash_password(body.password),
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return await _open_session(redis, account.user_id, display_name, response)


@router.post("/api/auth/login")
async def login(
    body: LoginBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    digest = username_digest(body.username)
    row = await db.execute(
        select(UserAccount).where(UserAccount.username_hash == digest)
    )
    account = row.scalar_one_or_none()
    # 用户名不存在与密码错误返回同一个错误，避免枚举账号。
    if not account or not verify_password(body.password, account.password_hash):
        raise api_error(401, "AUTH_INVALID_CREDENTIALS", "用户名或密码错误")
    display_name = (
        decrypt_text(account.display_name_enc)
        or decrypt_text(account.username_enc)
        or "用户"
    )
    return await _open_session(redis, account.user_id, display_name, response)


@router.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    redis: Redis = Depends(get_redis),
):
    settings = get_settings()
    session_id = request.cookies.get(settings.auth_cookie_name)
    if session_id:
        await delete_session(redis, session_id)
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    return {"ok": True}
