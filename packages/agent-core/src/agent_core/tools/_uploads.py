"""上传载荷的共享解析器——一份规格进，内存字节出。

现在有几个工具需要把文件发到某处：浏览器工具把它推入 ``<input type=file>``
（或原生文件选择器），``api_request`` 把它作为 multipart 部件发送。支持三种
字节来源，三者都折叠为相同的内存表示：

* ``path``——项目工作区内的文件（``tests/fixtures/orders.csv``、``.tmp/media/
  ...`` 下的截图）；
* ``attachment``——用户附加到会话的文件，位于 API 的内存上传存储中，*从不*写入
  磁盘；
* ``inline``——调用方当场生成的内容（文本或 base64），使测试完全不碰文件系统
  即可合成其载荷。

目的地不是信任边界：``page.route`` 无法检查上传正文，``api_request`` 按设计向
客户配置的主机 POST。因此整个外泄守卫位于这里的来源侧——项目根限制、隐藏文件
拦截和字节上限——每个消费者经由 :func:`resolve_upload_specs` 即可免费获得。
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PathEscapeError, canonical, resolve_within
from ._media import media_type_for

# 单个上传文件。会话附件被 API 限制在 max_upload_size_mb（10MB），因此相同
# 上限的路径来源上传在智能体脑中保持一个一致数字。
MAX_UPLOAD_FILE_BYTES = 10 * 1024 * 1024
# 内联内容经 LLM 上下文传输（在工具调用中生成），因此给紧得多的上限——1MB 的
# base64 已是模型不得不产出的约 35 万 token 载荷。
MAX_INLINE_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_FILES = 10

_MIME_RE = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]*$")
# 文件名随 multipart 的 Content-Disposition 头传输并成为浏览器 File.name——
# 控制字符（尤其 CR/LF）绝不能存活。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME_CHARS = 255

_SOURCES = ("attachment", "path", "inline")


class UploadSourceError(ValueError):
    """上传规格无法解析为字节。"""


@dataclass(frozen=True)
class UploadPayload:
    """已解析的上传字节，加上目的地所需的元数据。"""

    name: str
    data: bytes
    mime_type: str
    source: str
    origin: str
    # multipart 表单字段名（api_request）。浏览器工具忽略。
    field: str = "file"


def _clean_name(raw: Any, *, fallback: str = "") -> str:
    """清洗调用方提供的文件名，用于请求头和 ``File.name``。"""
    text = str(raw or "").replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name).strip()
    if name in ("", ".", ".."):
        return fallback
    return name[:_MAX_NAME_CHARS]


def normalize_mime_type(raw: Any, name: str, *, text_default: bool = False) -> str:
    """调用方提供的 MIME 看起来合法时用它，否则从 *name* 推断。

    未校验的文本会进入 multipart 的 Content-Type 头（和浏览器 ``File.type``），
    那里残留的 CR/LF 是头注入向量——任何不像 ``type/subtype`` 的内容都回退到
    基于名称的猜测。与 test_generator 共享，后者把同样的值嵌入生成的规格。
    """
    if isinstance(raw, str) and _MIME_RE.match(raw.strip()):
        return raw.strip()
    inferred = media_type_for(name)
    if text_default and inferred == "application/octet-stream":
        return "text/plain"
    return inferred


def _mime_for(spec: dict[str, Any], name: str, *, text_default: bool = False) -> str:
    return normalize_mime_type(spec.get("mime_type") or spec.get("mimeType"), name, text_default=text_default)


def _media_parts(rel: str) -> list[str] | None:
    """把 ``.tmp/media/{会话}/{文件}`` 路径拆分为各段。"""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) != 4 or parts[:2] != [".tmp", "media"]:
        return None
    return parts


def _hidden_part(rel: str) -> str | None:
    """第一个隐藏路径组件，路径可接受时返回 None。

    从项目工作区读取任意点文件会把 ``.env``/``.git/config`` 作为上传载荷交给
    智能体，这正是注入指令会索要的东西。``.tmp`` 在首位时豁免，因为平台自己的
    临时树在那里——截图、下载和录像都通过它到达消费工具。
    """
    for i, part in enumerate(p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")):
        if part == ".." or not part.startswith("."):
            continue
        if i == 0 and part == ".tmp":
            continue
        return part
    return None


def _resolve_path(context, spec: dict[str, Any], max_bytes: int) -> UploadPayload:
    rel = str(spec.get("path") or "").strip()
    if not rel:
        raise UploadSourceError("source='path' requires 'path' (workspace-relative)")

    # 虚拟媒体路径先交给上传存储：文件系统工具恰好在这个路径暴露用户附件，因此
    # 智能体可引用同一位置来复用刚读过的文件（与媒体路由 GET 相同的先存储后
    # 磁盘顺序）。
    parts = _media_parts(rel)
    if parts:
        lookup = getattr(context, "upload_file_lookup", None)
        if lookup is not None:
            data = lookup(parts[3])
            if data is not None:
                # 与磁盘路径相同的字节上限：同样的字节绝不能因哪个别名命名它们而
                # 被接受或拒绝。
                if len(data) > max_bytes:
                    raise UploadSourceError(
                        f"Upload {rel!r} is {_mb(len(data))}, above the {_mb(max_bytes)} limit"
                    )
                name = _clean_name(spec.get("name"), fallback=_clean_name(parts[3]))
                return UploadPayload(
                    name=name,
                    data=data,
                    mime_type=_mime_for(spec, name),
                    source="attachment",
                    origin=rel,
                )

    hidden = _hidden_part(rel)
    if hidden is not None:
        raise UploadSourceError(
            f"Hidden files and directories cannot be uploaded ({hidden!r}): {rel!r}"
        )
    try:
        full = resolve_within(context.project_fs_path, rel)
    except PathEscapeError as exc:
        raise UploadSourceError(f"Access denied: path is outside the project workspace ({rel!r})") from exc
    # 复检规范路径：名为 `innocuous`、指向 `.env` 的符号链接（或硬链接）在请求
    # 字符串里没有隐藏组件，但读取返回点文件的字节。
    resolved_rel = full.relative_to(canonical(context.project_fs_path)).as_posix()
    hidden = _hidden_part(resolved_rel)
    if hidden is not None:
        raise UploadSourceError(
            f"Hidden files and directories cannot be uploaded ({hidden!r}): {rel!r}"
        )
    if not full.is_file():
        raise UploadSourceError(f"File not found: {rel}")

    size = full.stat().st_size
    if size > max_bytes:
        raise UploadSourceError(
            f"Upload {rel!r} is {_mb(size)}, above the {_mb(max_bytes)} limit"
        )
    data = full.read_bytes()
    if len(data) > max_bytes:
        raise UploadSourceError(
            f"Upload {rel!r} is {_mb(len(data))}, above the {_mb(max_bytes)} limit"
        )
    name = _clean_name(spec.get("name"), fallback=_clean_name(rel))
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name),
        source="path",
        origin=rel,
    )


def _resolve_attachment(context, spec: dict[str, Any], max_bytes: int) -> UploadPayload:
    raw_name = str(spec.get("name") or "").strip()
    if not raw_name:
        raise UploadSourceError("source='attachment' requires 'name' (the attached filename)")
    lookup = getattr(context, "upload_file_lookup", None)
    if lookup is None:
        raise UploadSourceError(
            "Conversation attachments are not available here; use source='path' or source='inline'"
        )
    # 先逐字查名（存储以服务端保存的名为键），再按清洗形式查，使仅路径装饰不同的
    # 名仍能解析。
    cleaned = _clean_name(raw_name)
    data = None
    for candidate in dict.fromkeys([raw_name, cleaned]):
        data = lookup(candidate)
        if data is not None:
            break
    if data is None:
        names = getattr(context, "upload_file_names", None)
        available = sorted(names()) if names else []
        hint = ", ".join(available[:30]) if available else "none"
        raise UploadSourceError(f"Attachment {raw_name!r} not found. Available: {hint}")
    if len(data) > max_bytes:
        raise UploadSourceError(
            f"Attachment {raw_name!r} is {_mb(len(data))}, above the {_mb(max_bytes)} limit"
        )
    name = cleaned or _clean_name(raw_name, fallback="attachment")
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name),
        source="attachment",
        origin=raw_name,
    )


def _resolve_inline(context, spec: dict[str, Any], inline_max: int) -> UploadPayload:
    content = spec.get("content")
    content_b64 = spec.get("content_base64")
    if content is not None and content_b64 is not None:
        raise UploadSourceError("source='inline' takes either 'content' or 'content_base64', not both")
    if content_b64 is not None:
        if not isinstance(content_b64, str) or not _BASE64_RE.match(content_b64):
            raise UploadSourceError("content_base64 is not valid base64")
        try:
            # 带换行的 base64（CLI 编码器输出）通过上方字符门，但 validate=True
            # 拒绝它被允许携带的空白——严格解码前先去掉。
            data = base64.b64decode("".join(content_b64.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            # 绝不回显载荷——它是调用方内容而非凭证，但解码错误消息仍须保持无值。
            raise UploadSourceError("content_base64 is not valid base64") from exc
        text_default = False
    elif content is not None:
        data = str(content).encode("utf-8")
        text_default = True
    else:
        raise UploadSourceError("source='inline' requires 'content' or 'content_base64'")

    if len(data) > inline_max:
        raise UploadSourceError(
            f"Inline content is {_mb(len(data))}, above the {_mb(inline_max)} limit"
        )
    name = _clean_name(spec.get("name"))
    if not name:
        raise UploadSourceError("source='inline' requires 'name' (the filename to present)")
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name, text_default=text_default),
        source="inline",
        origin=name,
    )


def _infer_source(spec: dict[str, Any]) -> str:
    explicit = spec.get("source")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    if spec.get("content_base64") is not None or spec.get("content") is not None:
        return "inline"
    if spec.get("path"):
        return "path"
    if spec.get("name"):
        return "attachment"
    raise UploadSourceError("Provide one of 'path', 'name' (attachment), or 'content'/'content_base64'")


def resolve_upload_spec(
    context,
    spec: Any,
    *,
    max_bytes: int = MAX_UPLOAD_FILE_BYTES,
    inline_max_bytes: int = MAX_INLINE_BYTES,
) -> UploadPayload:
    """把一份上传规格解析为字节，失败抛 :class:`UploadSourceError`。"""
    if not isinstance(spec, dict):
        raise UploadSourceError(f"Each file must be an object, got {type(spec).__name__}")
    source = _infer_source(spec)
    if source == "path":
        payload = _resolve_path(context, spec, max_bytes)
    elif source == "attachment":
        payload = _resolve_attachment(context, spec, max_bytes)
    elif source == "inline":
        payload = _resolve_inline(context, spec, inline_max_bytes)
    else:
        raise UploadSourceError(f"Unknown upload source {source!r} (allowed: {', '.join(_SOURCES)})")
    field = spec.get("field")
    if isinstance(field, str) and field.strip():
        payload = UploadPayload(
            name=payload.name,
            data=payload.data,
            mime_type=payload.mime_type,
            source=payload.source,
            origin=payload.origin,
            field=field.strip(),
        )
    return payload


def resolve_upload_specs(
    context,
    specs: Any,
    *,
    max_bytes: int = MAX_UPLOAD_FILE_BYTES,
    inline_max_bytes: int = MAX_INLINE_BYTES,
    max_files: int = MAX_UPLOAD_FILES,
) -> list[UploadPayload]:
    """解析上传规格列表（含模式声明的数组与 LLM 的实际输出）。"""
    if not isinstance(specs, list):
        # 对应工具层其他地方的字符串化字典强制转换约定：JSON 字符串是值得精确指出
        # 的模型错误。
        if isinstance(specs, str):
            raise UploadSourceError("files must be an array, not a stringified JSON value")
        raise UploadSourceError(f"files must be an array, got {type(specs).__name__}")
    if not specs:
        raise UploadSourceError("files must contain at least one entry")
    if len(specs) > max_files:
        raise UploadSourceError(f"Too many files: {len(specs)} (limit {max_files} per call)")
    return [
        resolve_upload_spec(
            context, spec, max_bytes=max_bytes, inline_max_bytes=inline_max_bytes
        )
        for spec in specs
    ]


def _mb(num_bytes: int) -> str:
    """供错误消息使用的人类可读大小。"""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f}MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}KB"
    return f"{num_bytes}B"
