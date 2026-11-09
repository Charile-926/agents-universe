"""文件系统工具——在项目范围内读取/写入/列举文件。"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..definition_check import check_definition, project_slug_from_workspace
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# 字节上限：读取会直接喂给 LLM 上下文（2MB 在那里已经很大）；写入设上限以约束
# 磁盘占用。
_MAX_READ_BYTES = 2_000_000
_MAX_WRITE_BYTES = 5_000_000


def _definition_check(
    rel_path: str, content: str, context: ToolContext, scope: str
) -> dict[str, Any] | None:
    """定义文件的建议性校验块，非定义文件返回 None。

    智能体/技能/工作流的注册在之后发生且会静默失败，因此写入方是唯一还能修复文件
    的一方——它随写入结果拿到判定，而不必去猜。
    """
    return check_definition(
        rel_path,
        content,
        scope=scope,
        project_slug=project_slug_from_workspace(context.project_fs_path) if scope == "project" else None,
        project_fs_path=context.project_fs_path or None,
        framework_root=context.framework_root or None,
    )


def _attach_definition_check(
    result: dict[str, Any], rel_path: str, context: ToolContext, scope: str
) -> dict[str, Any]:
    """把建议性校验块附加到读取结果上；读取内容本身保持不变。"""
    content = result.get("content")
    if isinstance(content, str):
        check = _definition_check(rel_path, content, context, scope)
        if check is not None:
            result["definition_check"] = check
    return result


def _normalize_rel_path(rel_path: str, project_fs_path: str) -> str:
    """把错误的绝对路径或带项目前缀的路径，剥离为相对工作区的形式。

    模型有时会输出这样的路径：
      - /app/projects/slug/tests/...  （容器内绝对路径）
      - projects/slug/tests/...       （带项目前缀的相对仓库路径）
      - slug/tests/...                （裸 slug 前缀）
    都应解析为：tests/...
    """
    base = Path(project_fs_path).resolve()
    slug = base.name

    normalized = rel_path.replace("\\", "/")

    # 剥离与工作区匹配的绝对路径前缀（解析形式或原始形式）。
    for prefix in (
        str(base).replace("\\", "/") + "/",
        project_fs_path.replace("\\", "/").rstrip("/") + "/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            return normalized or rel_path
        stripped = normalized.lstrip("/")
        prefix_no_slash = prefix.lstrip("/")
        if stripped.startswith(prefix_no_slash):
            normalized = stripped[len(prefix_no_slash):]
            return normalized or rel_path

    normalized = normalized.lstrip("/")

    # 剥离 "projects/slug/" 前缀——仅当不存在真实的 projects/{slug}/ 子目录时；
    # 模型可能确实指该子目录（如工作区内的 monorepo 检出）。
    if not (base / "projects" / slug).is_dir():
        prefix_pattern = re.compile(rf"^projects/{re.escape(slug)}/", re.IGNORECASE)
        normalized = prefix_pattern.sub("", normalized)

    # 剥离裸 "slug/" 前缀——同样的守卫：存在真实的顶层 {slug}/ 子目录时，
    # `web/index.html` 指该子目录，而不是要剥离的路径前缀（剥离会静默读/写/删错误
    # 的文件）。
    if normalized.startswith(f"{slug}/") and not (base / slug).is_dir():
        normalized = normalized[len(slug) + 1:]

    return normalized or rel_path


def _read_file(target: Path, rel_path: str) -> dict[str, Any]:
    try:
        # 读取前先做字节上限检查：超大文件会导致 OOM 并撑爆 LLM 上下文（对应
        # api_request 的 5MB 上限）。
        if target.stat().st_size > _MAX_READ_BYTES:
            return {"error": f"Cannot read {rel_path}: file too large ({target.stat().st_size} bytes > {_MAX_READ_BYTES})"}
        # BOM（VS Code 保存时会带）会粘在内容上，破坏期望干净 markdown/文本的
        # 消费方。
        content = target.read_text(encoding="utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        _log.warning("filesystem read_file: not UTF-8: %s", rel_path)
        return {"error": f"Cannot read {rel_path}: file is not valid UTF-8 (binary?)"}
    except PermissionError:
        _log.warning("filesystem read_file: permission denied: %s", rel_path)
        return {"error": f"Permission denied: {rel_path}"}
    except OSError as e:
        _log.warning("filesystem read_file: OSError on %s: %s", rel_path, e)
        return {"error": f"Cannot read {rel_path}: {e}"}
    return {"content": content, "path": rel_path, "size_bytes": len(content.encode())}


def _list_dir(target: Path, rel_path: str) -> dict[str, Any]:
    if not target.exists():
        return {"error": f"Directory not found: {rel_path}"}
    if not target.is_dir():
        return {"error": f"Not a directory: {rel_path}"}
    entries = []
    for entry in sorted(target.iterdir()):
        entries.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size_bytes": entry.stat().st_size if entry.is_file() else 0,
        })
    return {"entries": entries, "count": len(entries)}


def _media_parts(rel_path: str) -> list[str] | None:
    """把 .tmp/media/... 路径拆成各段，不是该路径则返回 None。

    用户附件从不写入磁盘——它们在本轮期间存在于 API 的内存上传存储中。文件系统
    工具通过 ToolContext 的可选上传钩子提供这些字节。
    """
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[:2] != [".tmp", "media"]:
        return None
    return parts


def _upload_store_lookup(parts: list[str], context: ToolContext) -> bytes | None:
    """从存储中取出 .tmp/media/{会话}/{文件} 路径对应的字节。"""
    if len(parts) != 4:
        return None
    lookup = getattr(context, "upload_file_lookup", None)
    return lookup(parts[3]) if lookup else None


def _upload_store_names(parts: list[str], context: ToolContext) -> list[str]:
    """.tmp/media/{会话} 目录下的上传文件名。"""
    if len(parts) != 3:
        return []
    names = getattr(context, "upload_file_names", None)
    return sorted(names()) if names else []


class FilesystemTool(Tool):
    name = "filesystem"
    prompt_hint = (
        "Preferred tool for ALL file reads, writes, and directory listings — never use "
        "shell cat/head/tail/echo>/sed for reading or writing files. It has no content "
        "search; shell grep/find is acceptable for searching only. Writing or reading an "
        "agent/skill/workflow definition returns a `definition_check` block: when `ok` is "
        "false the file will NOT be registered — fix the reported errors and rewrite the "
        "file before reporting the task as done."
    )
    description = (
        "Read, write, list, or delete files within the current project directory. "
        "All paths must be relative to the project root or knowledge directory. "
        "Writes and reads of definition files (agents/*.agent.md, skills/*.md, "
        "workflows/*.workflow.md) also return an advisory `definition_check` block."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read_file", "write_file", "list_dir", "delete_file", "create_dir"],
                "description": "The filesystem operation to perform",
            },
            "path": {
                "type": "string",
                "description": "Relative path from the project root",
            },
            "content": {
                "type": "string",
                "description": "Content to write (for write_file only)",
            },
        },
        "required": ["operation", "path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        rel_path = _normalize_rel_path(params["path"], context.project_fs_path)

        # 写入 .git（钩子、配置）会安装 git 在后续命令中不经校验就执行的代码，或让
        # 配置重指 hooksPath——shell 沙箱会拒绝这些目标，文件系统工具也必须如此
        # （嵌套仓库：subdir/.git/hooks；子模块：.git/modules/<name>/hooks）。
        # 从 .git 读取仍允许。
        if operation in ("write_file", "delete_file", "create_dir"):
            _git_rel = rel_path.replace("\\", "/")
            # 不区分大小写：在 Windows/macOS 上 `.GIT/hooks/x` 与 `.git/hooks/x`
            # 是同一目录，因此精确匹配检查会让守卫被大小写绕过。
            if any(part.lower() == ".git" for part in _git_rel.split("/")):
                return {"error": ".git 目录为只读：hooks/config 会被 git 在后续命令中执行，请写到项目其他位置"}

        # 框架目录（agents/、workflows/、knowledge/_template/）从框架根读取。
        # agents/ 和 workflows/ 是覆盖路径：项目工作区遮蔽全局副本——读取优先工作区，
        # 写入路由到工作区。knowledge/_template 严格只读。
        _is_framework_path = (
            rel_path.startswith("agents/")
            or rel_path.startswith("workflows/")
            or rel_path == "knowledge/_template"
            or rel_path.startswith("knowledge/_template/")
        )
        if _is_framework_path and context.framework_root:
            _is_shadowed = rel_path.startswith("agents/") or rel_path.startswith("workflows/")
            if operation in ("write_file", "delete_file", "create_dir"):
                # 被遮蔽的写入落到下方的项目范围处理（它们属于工作区，绝不属框架）。
                if not _is_shadowed:
                    return {"error": f"{rel_path.split('/')[0]}/ directory is read-only"}
            else:
                if _is_shadowed:
                    # 读取优先项目工作区副本（若存在）。
                    ws_base = Path(context.project_fs_path).resolve()
                    ws_target = (ws_base / rel_path).resolve()
                    if ws_target.is_relative_to(ws_base) and ws_target.exists():
                        if operation == "list_dir":
                            return _list_dir(ws_target, rel_path)
                        return _attach_definition_check(
                            _read_file(ws_target, rel_path), rel_path, context, "project"
                        )
                if operation not in ("read_file", "list_dir"):
                    return {"error": f"{rel_path.split('/')[0]}/ directory is read-only"}
                target = (Path(context.framework_root) / rel_path).resolve()
                fw_root = Path(context.framework_root).resolve()
                # 包含检查必须针对路径所指的框架“目录”，而非 framework_root 本身：
                # 当 PROJECTS_ROOT 嵌套在其下时，`agents/../projects/other-slug/.env`
                # 会解析到 framework_root 内部并读取兄弟工作区，破坏项目隔离边界。
                if rel_path.startswith("knowledge/_template"):
                    scope_root = fw_root / "knowledge" / "_template"
                else:
                    scope_root = fw_root / rel_path.split("/", 1)[0]
                if not target.is_relative_to(scope_root):
                    return {"error": f"Access denied: path {rel_path!r} is outside allowed scope"}
                if not target.exists():
                    if operation == "list_dir":
                        return {"error": f"Directory not found: {rel_path}"}
                    parent = target.parent
                    hint: dict[str, Any] = {"error": f"File not found: {rel_path}"}
                    if parent.is_dir():
                        hint["available_in_parent"] = sorted(e.name for e in parent.iterdir())[:30]
                    return hint
                if operation == "list_dir":
                    return _list_dir(target, rel_path)
                return _attach_definition_check(
                    _read_file(target, rel_path), rel_path, context, "global"
                )

        # 安全：解析并校验路径位于项目范围内。
        base = Path(context.project_fs_path).resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            return {"error": f"Access denied: path {rel_path!r} is outside project scope"}
        # 上面原始路径的 .git 守卫会被工作区符号链接绕过（某些克隆保留了
        # hooks -> .git/hooks）：字面路径没有 .git 成分，但 resolve() 跟随链接进入
        # .git——重新检查解析后的各成分，否则写入会落到 .git/hooks。
        if operation in ("write_file", "delete_file", "create_dir"):
            _git_resolved = target.relative_to(base).parts
            if any(part.lower() == ".git" for part in _git_resolved):
                return {"error": ".git 目录为只读：hooks/config 会被 git 在后续命令中执行，请写到项目其他位置"}

        if operation == "read_file":
            parent = target.parent
            if target.exists():
                return _attach_definition_check(
                    _read_file(target, rel_path), rel_path, context, "project"
                )
            # 用户附件存在于内存上传存储中（从不在磁盘上）——提供这些字节，使
            # 行内预览被截断或省略时，智能体能重新读取完整内容。
            media_parts = _media_parts(rel_path)
            store_data = _upload_store_lookup(media_parts, context) if media_parts else None
            if store_data is not None:
                if len(store_data) > _MAX_READ_BYTES:
                    return {"error": f"Cannot read {rel_path}: file too large ({len(store_data)} bytes > {_MAX_READ_BYTES})"}
                try:
                    content = store_data.decode("utf-8")
                except UnicodeDecodeError:
                    _log.warning("filesystem read_file: store upload not UTF-8: %s", rel_path)
                    return {"error": f"Cannot read {rel_path}: file is not valid UTF-8 (binary?)"}
                return {"content": content, "path": rel_path, "size_bytes": len(content.encode())}
            hint: dict[str, Any] = {"error": f"File not found: {rel_path}"}
            names = []
            if parent.is_dir():
                names = sorted(e.name for e in parent.iterdir())
            if media_parts:
                names = sorted(set(names + _upload_store_names(media_parts[:-1], context)))
            if names:
                hint["available_in_parent"] = names[:30]
            return hint

        elif operation == "write_file":
            # `null` 转为 ""（键存在，所以默认值不会触发，且 encode() 会报错）；其他
            # 非字符串做 JSON 序列化。若用 `or ""`，会对假值非字符串（0、{}、[]、
            # False）静默把文件截断为空，却仍上报成功。
            raw_content = params.get("content")
            if raw_content is None:
                content = ""
            elif isinstance(raw_content, str):
                content = raw_content
            else:
                try:
                    content = json.dumps(raw_content, ensure_ascii=False)
                except (TypeError, ValueError):
                    return {"error": f"Cannot write {rel_path}: content must be a string or JSON-serializable value"}
            if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
                return {"error": f"Cannot write {rel_path}: content too large ({len(content)} chars > {_MAX_WRITE_BYTES} bytes)"}
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(content, encoding="utf-8")
            except PermissionError:
                _log.warning("filesystem write_file: permission denied: %s", rel_path)
                return {"error": f"Permission denied: {rel_path}"}
            except OSError as e:
                _log.warning("filesystem write_file: OSError on %s: %s", rel_path, e)
                return {"error": f"Cannot write {rel_path}: {e}"}
            result: dict[str, Any] = {
                "success": True,
                "path": rel_path,
                "bytes_written": len(content.encode()),
            }
            # 无论如何文件都已在磁盘上；校验失败意味着它不会注册，因此调用方必须
            # 重写，而不是上报成功。
            check = _definition_check(rel_path, content, context, "project")
            if check is not None:
                result["definition_check"] = check
            return result

        elif operation == "list_dir":
            media_parts = _media_parts(rel_path)
            store_names = _upload_store_names(media_parts, context) if media_parts else []
            if not target.exists() and not store_names:
                return {"error": f"Directory not found: {rel_path}"}
            if target.exists() and not target.is_dir():
                return {"error": f"Not a directory: {rel_path}"}
            entries = []
            if target.is_dir():
                for entry in sorted(target.iterdir()):
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size_bytes": entry.stat().st_size if entry.is_file() else 0,
                    })
            if store_names:
                # 把内存中的上传合并进媒体目录列表。
                lookup = getattr(context, "upload_file_lookup", None)
                for name in store_names:
                    data = lookup(name) if lookup else None
                    entries.append({
                        "name": name,
                        "type": "file",
                        "size_bytes": len(data) if data else 0,
                    })
                entries.sort(key=lambda e: e["name"])
            return {"entries": entries, "count": len(entries)}

        elif operation == "delete_file":
            if not target.exists():
                return {"error": f"File not found: {rel_path}"}
            if target.is_dir():
                return {"error": f"Not a file: {rel_path}"}
            try:
                target.unlink()
            except OSError as e:
                return {"error": f"Failed to delete {rel_path}: {e.strerror or e}"}
            return {"success": True, "deleted": rel_path}

        elif operation == "create_dir":
            # mkdir(exist_ok=True) 对已存在的目录是空操作，但当路径是已存在的普通
            # 文件时会抛 FileExistsError。
            if target.is_file():
                return {"error": f"Cannot create directory {rel_path}: a file with that name already exists"}
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"error": f"Cannot create directory {rel_path}: {e}"}
            return {"success": True, "path": rel_path}

        return {"error": f"Unknown operation: {operation}"}
