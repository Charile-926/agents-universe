"""用户敏感字段的加密与口令哈希。

设计目标：数据库里不出现可读的用户名与显示名，也不出现可逆或可离线爆破的
口令存储。

- **口令**：PBKDF2-HMAC-SHA256，每用户独立随机盐，迭代 20 万次，存储格式
  ``pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>``；校验使用常数时间比较。
- **用户名 / 显示名**：Fernet（AES-128-CBC + HMAC-SHA256）加密后存储。
  密钥不落库，由 ``SECRET_KEY`` 经 HKDF-SHA256 派生，并按用途分离
  （``field-encryption`` / ``username-index``）。
- **用户名查找**：另存 HMAC-SHA256 摘要（大小写归一化），用于唯一约束与登录
  查询；摘要不可逆，且没有 ``SECRET_KEY`` 无法离线枚举。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from api.config import get_settings

_PBKDF2_ITERATIONS = 200_000
_HKDF_SALT = b"agents-universe/user-fields/v1"


def _derive(info: bytes, length: int = 32) -> bytes:
    """从 SECRET_KEY 派生用途隔离的子密钥。"""
    secret = get_settings().secret_key.encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=_HKDF_SALT,
        info=info,
    ).derive(secret)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive(b"field-encryption")))


def encrypt_text(value: str) -> str:
    """加密一个用户字段，返回可直接入库的字符串。"""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str | None) -> str:
    """解密用户字段；空值或历史明文/损坏数据返回空串，不抛异常。"""
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return ""


def username_digest(username: str) -> str:
    """用户名的 HMAC 摘要（大小写与首尾空白归一化），用于唯一约束与登录查找。"""
    normalized = username.strip().casefold().encode("utf-8")
    key = _derive(b"username-index")
    return hmac.new(key, normalized, hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False
