"""deliver_file 工具——把项目工作区文件交付给用户。

把文件复制到会话的媒体目录，/api/media 以按用户鉴权方式对外提供（仅会话所有者
可下载）。已在媒体目录内的文件原样返回，不复制。
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ._media import media_type_for, media_url, sanitize_suffix
from .base import Tool, ToolContext

# 与 routers/media.py 白名单（及其单独的 ".." 拒绝）保持一致：已在媒体目录里的
# 文件仍必须可被服务，否则交给模型的 URL 必然 400。
_SERVABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")


def _servable_media_name(name: str) -> bool:
    return bool(_SERVABLE_NAME_RE.match(name)) and ".." not in name


class DeliverFileTool(Tool):
    name = "deliver_file"
    prompt_hint = (
        "Use to hand the user a file from the workspace (shows up as a downloadable "
        "attachment in chat). Files written to the code_executor OUTPUT_DIR are "
        "delivered automatically; this tool covers everything else."
    )
    description = (
        "Deliver a file from the project workspace to the user as a downloadable "
        "attachment in the conversation. The path is workspace-relative. Files "
        "written to the code_executor OUTPUT_DIR are already delivered "
        "automatically — use this for other files (reports written with "
        "write_file, test scripts, shell output). Only files inside the project "
        "workspace can be delivered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path of the file to deliver, e.g. reports/summary.csv",
            },
        },
        "required": ["path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        path = str(params.get("path", "")).strip()
        if not path:
            return {"error": "path is required"}

        project_root = Path(context.project_fs_path).resolve()
        src = (project_root / path).resolve()
        if not src.is_relative_to(project_root):
            return {"error": f"path escapes the project workspace: {path}"}
        if not src.is_file():
            return {"error": f"file not found: {path}"}

        media_dir = Path(context.conversation_media_dir)
        if src.parent == media_dir.resolve() and _servable_media_name(src.name):
            dest = src  # 已在被服务的媒体目录——不复制
        else:
            # 智能体以不可服务的名（空格、前导点、非 ASCII）写进媒体目录的文件，
            # 仍需在生成的名下复制——返回它自己的名会产生每个 /api/media 请求都
            # 拒绝的 URL。
            media_dir.mkdir(parents=True, exist_ok=True)
            dest = media_dir / f"{uuid.uuid4().hex[:8]}{sanitize_suffix(src.name)}"
            try:
                shutil.copy2(str(src), str(dest))
            except OSError as exc:
                return {"error": f"failed to copy {path}: {exc}"}

        record = {
            "id": uuid.uuid4().hex[:8],
            "url": media_url(context, dest.name),
            "name": src.name[:255],
            "media_type": media_type_for(src.name),
            "size": dest.stat().st_size,
        }
        return {"files": [record]}
