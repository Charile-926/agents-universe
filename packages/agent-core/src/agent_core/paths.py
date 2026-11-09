"""跨项目隔离共用的路径安全辅助函数。

零依赖模块，agent-core 与 api 包都会导入。所有比较都经过 canonical()，因此
Windows 盘符大小写和 8.3 短文件名无法骗过单纯基于 resolve() 的检查。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 知识 slug 校验，由 agent_core.tools.knowledge_rw 和 API 知识路由共享。含大写
# 字母和点号的分层 slug（如 "API-Gateway"、"notes.v2"）是合法的——索引器直接
# 从文件名派生 slug。拒绝：任何位置的 "." / ".." 段（目录穿越）、反斜杠
# （Windows 分隔符变体），以及开头的非字母数字字符。
KNOWLEDGE_SLUG_RE = re.compile(r"^(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9][A-Za-z0-9_./-]*$")


class PathEscapeError(ValueError):
    """解析出的路径逃逸出允许的基目录时抛出。"""


def canonical(p: str | Path) -> Path:
    """返回 *p* 可跨平台比较的规范化形式。

    realpath 解析符号链接并规范化路径；normcase 在 Windows 上统一盘符/路径
    大小写，使比较可靠。
    """
    return Path(os.path.normcase(os.path.realpath(os.fspath(p))))


def is_within(base: str | Path, target: str | Path) -> bool:
    """当 canonical(target) 位于 canonical(base) 内（含 base 本身）时为真。"""
    base_c = canonical(base)
    target_c = canonical(target)
    return target_c == base_c or base_c in target_c.parents


def resolve_within(base: str | Path, rel: str | Path) -> Path:
    """把 *rel* 拼到 *base* 上，并要求结果留在 *base* 内。

    若规范化路径逃逸出基目录（如经 ``..`` 段或绝对路径 *rel*），抛出
    PathEscapeError。
    """
    rel_s = os.fspath(rel)
    # 盘符（"C:\evil"）/ UNC（"\\server"、"//server"）前缀在 Windows 上是绝对
    # 路径，在 POSIX 上却只是普通相对目录——一次逃逸尝试会静默解析到一个名为
    # "C:" 的文件夹并返回 404，而不是 400。在所有主机上都拒绝该前缀，使行为
    # 不依赖操作系统。
    if re.match(r"^[A-Za-z]:[\\/]", rel_s) or rel_s.startswith(("\\", "//")):
        raise PathEscapeError(f"Path escapes allowed base {base}: {rel!r}")
    base_c = canonical(base)
    candidate = canonical(base_c / rel)
    if candidate != base_c and base_c not in candidate.parents:
        raise PathEscapeError(f"Path escapes allowed base {base_c}: {rel!r}")
    return candidate
