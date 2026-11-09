"""智能体工具的令牌取回与解密。

独立实现——不从 api 包导入。
采用与 api/services/token_vault.py 相同的 AES-256-GCM 方案。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from .base import ToolContext

_log = logging.getLogger(__name__)

_ITERATIONS = 100_000
_KEY_LEN = 32


class ToolAuthError(Exception):
    """所需令牌缺失或解密失败时抛出。"""

    def __init__(self, service_key: str, detail: str = "", *, message: str | None = None):
        self.service_key = service_key
        if message is not None:
            # 令牌存在但无法原样使用（如头值里粘贴带来的尾随换行）——默认的
            # “Token not configured”前缀会告诉用户相反的信息，因此由调用方提供
            # 完整消息。
            super().__init__(message)
            return
        msg = f"Token not configured: '{service_key}'. Ask the user to add it in Settings → Integrations."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


def _get_secret_key(context: "ToolContext | None" = None) -> str:
    # 优先用注入 ToolContext 的值（由 API 从 pydantic 设置设定），即使 os.environ
    # 未设置，也能尊重 pydantic 的 .env 加载。
    if context is not None and getattr(context, "secret_key", ""):
        return context.secret_key
    key = os.environ.get("SECRET_KEY", "")
    if not key:
        raise RuntimeError("SECRET_KEY env var not set — cannot decrypt tokens")
    return key


def _derive_key(secret_key: str, user_id: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode(),
        user_id.encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )


def _decrypt(ciphertext: str, user_id: str, context: "ToolContext | None" = None) -> str:
    key = _derive_key(_get_secret_key(context), user_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


async def get_token(context: ToolContext, service_key: str) -> str:
    """为当前用户取回并解密单个令牌。

    经 context.db_session 从 user_tokens 表读取。
    未找到或解密失败时抛出 ToolAuthError。
    """
    if not context.db_session:
        raise ToolAuthError(service_key, "no database session available")

    result = await context.db_session.execute(
        text(
            "SELECT encrypted_value FROM user_tokens "
            "WHERE user_id = :uid AND service_key = :skey"
        ),
        {"uid": context.user_id, "skey": service_key},
    )
    row = result.first()
    if not row:
        raise ToolAuthError(service_key)

    try:
        return _decrypt(row[0], context.user_id, context)
    except RuntimeError as e:
        raise ToolAuthError(service_key, str(e))
    except Exception:
        raise ToolAuthError(service_key, "decryption failed — token may be corrupted")


async def get_tokens(context: ToolContext, *service_keys: str) -> dict[str, str]:
    """取回并解密多个令牌。遇到第一个缺失键即抛出。"""
    tokens = {}
    for key in service_keys:
        tokens[key] = await get_token(context, key)
    return tokens


async def get_token_optional(context: ToolContext, service_key: str) -> str | None:
    """同 get_token，但缺失时返回 None 而非抛出。"""
    try:
        return await get_token(context, service_key)
    except ToolAuthError:
        return None


# ---------------------------------------------------------------------------
# 项目范围的密钥解析器
# 优先级：project_secrets → user_tokens → ToolAuthError
# ---------------------------------------------------------------------------

def _derive_project_key(secret_key: str, project_id: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode(),
        f"project:{project_id}".encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )


def _decrypt_project_secret(ciphertext: str, project_id: str, context: "ToolContext | None" = None) -> str:
    key = _derive_project_key(_get_secret_key(context), project_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


async def get_secret(context: ToolContext, service_key: str, *, environment: str | None = None) -> str:
    """取回密钥：先项目级，再回退到用户级。

    项目密钥属于项目，任何有项目访问权限的用户都可访问。用户令牌是旧配置的逐用户
    回退。
    """
    if not context.db_session:
        raise ToolAuthError(service_key, "no database session available")

    # 1. 尝试 project_secrets——先精确匹配环境，再回退到与环境无关的项。
    # is_active 绑定布尔值（不用 = 1）：SQL Server 存 BIT，PostgreSQL 存
    # BOOLEAN——字面比较是方言相关的。
    base_params: dict = {"pid": context.project_id, "skey": service_key, "active": True}

    candidates: list[tuple[str, dict]] = []
    if environment:
        candidates.append(("AND environment = :env", {**base_params, "env": environment}))
    # 始终把与环境无关的项作为回退尝试（覆盖未带环境标签保存的令牌）。保存路径把
    # 缺失环境规范化为 ""——SQL Server 唯一索引把 NULL 视为不同值，因此新行存
    # ''——但旧行存 NULL。两者都匹配。
    candidates.append(("AND (environment IS NULL OR environment = '')", base_params))

    for env_clause, params in candidates:
        result = await context.db_session.execute(
            text(
                "SELECT encrypted_value FROM project_secrets "
                f"WHERE project_id = :pid AND service_key = :skey {env_clause} "
                "AND is_active = :active AND secret_name = 'default' "
                # 旧的重复行可能残留（同一键的 NULL + "" 行）——优先 "" 行。DESC 下
                # NULL 的排序因方言而异（PostgreSQL 把 NULL 放最前），因此显式按是否
                # NULL 排序——用 CASE，而非裸谓词：T-SQL 拒绝在布尔表达式上做
                # ORDER BY。
                "ORDER BY CASE WHEN environment IS NULL THEN 1 ELSE 0 END ASC, "
                "environment DESC"
            ),
            params,
        )
        row = result.first()
        if row:
            _log.warning(
                "get_secret: hit project_secrets key=%r env_clause=%r project_id=%r",
                service_key, env_clause, context.project_id,
            )
            try:
                return _decrypt_project_secret(row[0], context.project_id, context)
            except Exception:
                raise ToolAuthError(service_key, "project secret decryption failed")

    # 2. 回退到 user_tokens（旧方式）
    _log.warning(
        "get_secret: project_secrets miss for ALL candidates — service_key=%r environment=%r project_id=%r; falling back to user_tokens",
        service_key, environment, context.project_id,
    )
    token = await get_token(context, service_key)
    _log.warning(
        "get_secret: hit user_tokens key=%r project_id=%r",
        service_key, context.project_id,
    )
    return token


async def get_secret_optional(context: ToolContext, service_key: str, *, environment: str | None = None) -> str | None:
    """同 get_secret，但缺失时返回 None 而非抛出。"""
    try:
        return await get_secret(context, service_key, environment=environment)
    except ToolAuthError:
        return None
