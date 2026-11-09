"""把工作区文件转为 /api/media 记录的共享辅助函数。

API 的媒体路由（api/routers/media.py）只服务文件名匹配严格白名单的文件——
必须使用服务端生成的名，客户端文件名放在记录的 ``name`` 字段里。向用户交付文件
的工具（code_executor 输出、deliver_file）在这里共享清洗与 MIME 推断，使每个
产出的 URL 都可被服务。

URL 由 :func:`media_url` 产出：当工具上下文带 ``app_base_url``（由 API 层从
``APP_BASE_URL`` 注入）时，记录带完全限定 URL（``https://host/agent/api/media/
...``），使把 URL 引用进回复的 LLM 给用户一个可直接使用的下载地址。
``app_root_path``（``APP_ROOT_PATH``）在设置时追加，覆盖部署在子路径（如
``/agent``）之后、浏览器必须访问 ``https://host/agent/api/media/...`` 的情况。
未配置 base URL 时返回历史相对路径，保持本地/开发/测试行为不变。

LLM 即使被告知工具 URL 已是绝对，偶尔仍会在前面再加 base URL（模型把
``https://host/agent/api/media/...`` 误当相对路径，写成
``https://host/agenthttps://host/agent/api/media/...``）。
:func:`normalize_media_urls` 是渲染/持久化安全网，折叠那个重复的 base，使引用的
链接保持可用。
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

# 对应 API 白名单：字母数字/连字符/下划线/点，不允许前导点。后缀（清洗后）必须像
# upload_media 一样纯字母数字。
_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 对下载重要的已推断类型；任何未映射的回退到 mimetypes.guess_type，再回退到
# application/octet-stream。
_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
}

_MEDIA_MARKER = "/api/media/"

# 媒体标记正前方重复的绝对 base：``<base><base>/api/media/...``（损坏形态）对
# ``<base>/api/media/...``（正确形态）。分组 1 捕获一个 base 副本；反向引用要求
# 紧随其后有完全相同的第二个副本，先行断言保证折叠形态只应用于媒体 URL（绝不应用于
# 恰好重复主机名的任意文本）。``re.sub(..., r"\\1", text)`` 保留第一个副本并
# 丢弃重复。
_DUPLICATED_BASE_RE = re.compile(
    r"(https?://[A-Za-z0-9._~-]+(?::\d+)?(?:/[A-Za-z0-9._~-]+)*)"
    r"\1(?=/api/media/)"
)


def sanitize_suffix(filename: str) -> str:
    """小写、纯字母数字的后缀（≤16 字符），对媒体白名单安全；无可用内容（缺
    扩展名或怪异）时返回 ''。"""
    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 16 or not _SAFE_SUFFIX_RE.match(suffix[1:]):
        return ""
    return suffix


def media_type_for(filename: str) -> str:
    """交付文件的 MIME 类型——先覆盖表，再 mimetypes，最后二进制回退（浏览器
    下载 octet-stream）。"""
    suffix = Path(filename).suffix.lower()
    override = _MIME_OVERRIDES.get(suffix)
    if override:
        return override
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def media_url(context, filename: str) -> str:
    """为会话媒体目录中的 *filename* 构建媒体记录 URL。

    工具上下文带 ``app_base_url`` 时返回绝对 URL（``{app_base_url}[/{app_root_path}]
    /api/media/...``）——LLM 可逐字引用为可用下载地址，包括部署子路径（``/agent``
    等），使浏览器访问被代理的位置。未配置 base URL（本地/开发/测试）时回退到相对的
    ``/api/media/...`` 路径。
    """
    rel = f"{_MEDIA_MARKER}{context.project_id}/{context.conversation_id}/{filename}"
    base = getattr(context, "app_base_url", "") or ""
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return rel
    root = getattr(context, "app_root_path", "") or ""
    root = root.strip("/")
    if root and not base.endswith(f"/{root}"):
        base = f"{base}/{root}"
    return f"{base}{rel}"


def normalize_media_urls(text: str) -> str:
    """折叠 *text* 中 /api/media/ 链接周围重复的 base URL。

    工具层发出完整绝对 URL（``media_url``），但 LLM 仍可能把它当相对路径并再次
    前置 base，产生 ``https://host/agenthttps://host/agent/api/media/...``。本函数
    把 base 在 ``/api/media/`` 标记正前方重复的任何媒体 URL 改写为单一规范形态。
    非媒体 URL 和已正确的 URL 原样通过。

    在 API 持久化层应用（保持会话历史干净，使后续轮次看不到损坏形态），也在前端
    渲染层应用（实时修复已持久化的消息）。
    """
    if not text or _MEDIA_MARKER not in text:
        return text
    return _DUPLICATED_BASE_RE.sub(r"\1", text)
