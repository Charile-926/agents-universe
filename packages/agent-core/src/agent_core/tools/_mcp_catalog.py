"""MCP 服务器注册表加载器。

两级注册表（见 ``knowledge/_template/mcp-servers.md``）：
- **全局**服务器位于 ``mcp_servers`` 表（``project_id`` 为 NULL），经 REST
  CRUD 端点和设置界面管理。
- **项目**服务器位于项目的 ``integrations/mcp-servers`` 知识文件，由
  ``api/services/mcp_sync.py`` 惰性同步进同一张表（集成专家写文件；同步是像项目
  智能体一样的投影）。

运行时只读表：``load_mcp_servers`` 查询 ``project_id = :current OR
project_id IS NULL``，让项目行遮蔽同 sanitized slug 的全局行（对应智能体/技能/
工作流的遮蔽规则）。``load_mcp_servers_from_dir`` 保留给同步路径（文件 → 表）
和离线测试。

解析刻意宽松：格式错误的目录块不会破坏整个文件，缺失/无法读取的文件产生空字典。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from sqlalchemy import text

from .base import ToolContext

_log = logging.getLogger(__name__)

MCP_CATALOG_SLUG = "integrations/mcp-servers"

_YAML_FENCE_RE = re.compile(
    r"^```ya?ml\s*\n(.*?)```", re.DOTALL | re.MULTILINE | re.IGNORECASE
)

_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]")

# 目录条目键映射到 mcp_servers 专用列，而非 options JSON 列。两个列表与
# api/services/mcp_sync.py 保持对称。
_CFG_COLUMN_KEYS = frozenset({
    "slug", "name", "description", "transport", "url", "headers", "enabled",
})
_CFG_OPTION_KEYS = frozenset({
    "allowed_hosts", "allow_private_network", "connect_timeout_seconds",
    "call_timeout_seconds", "tools", "require_confirmation_for_write",
    "redact_response",
})
_CFG_AUTH_KEYS = frozenset({"type", "secret_ref", "secret_scope", "header_name", "value_template"})


def sanitize_slug(slug: str) -> str:
    """把服务器 slug 规范化为合法工具名片段。"""
    return _SLUG_SANITIZE_RE.sub("_", slug.lower()).strip("_")


def _load_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def mcp_row_to_config(row: Any) -> dict[str, Any]:
    """把 ``mcp_servers`` 表行转换为运行时配置字典。

    结果形态与 ``load_mcp_servers_from_dir`` 过去为同一条目产出的一致，因此消费者
    （mcp_client）不关心服务器来自全局行、同步的项目行还是文件。
    """
    auth_type = (getattr(row, "auth_type", None) or "none").lower()
    auth: dict[str, Any] = {}
    if auth_type != "none":
        auth = {
            "type": auth_type,
            "secret_ref": getattr(row, "secret_ref", None),
            "secret_scope": getattr(row, "secret_scope", None) or "project",
        }
        if getattr(row, "auth_header_name", None):
            auth["header_name"] = row.auth_header_name
        if getattr(row, "auth_value_template", None):
            auth["value_template"] = row.auth_value_template

    cfg: dict[str, Any] = {
        "slug": getattr(row, "slug", ""),
        "name": getattr(row, "name", "") or getattr(row, "slug", ""),
        "transport": getattr(row, "transport", None) or "auto",
        "url": getattr(row, "url", None),
        "headers": _load_json_dict(getattr(row, "headers", None)),
        "auth": auth,
        "enabled": bool(getattr(row, "enabled", True)),
    }
    description = getattr(row, "description", None)
    if description:
        cfg["description"] = description
    cfg.update(_load_json_dict(getattr(row, "options", None)))
    return cfg


_MCP_TABLE_SELECT = (
    "SELECT project_id, slug, name, description, transport, url, headers, "
    "auth_type, secret_ref, secret_scope, auth_header_name, auth_value_template, "
    "options, enabled "
    "FROM mcp_servers"
)


async def load_mcp_servers(context: ToolContext) -> dict[str, dict[str, Any]]:
    """从表中为上下文所属项目加载已启用的 MCP 服务器。

    返回 ``{sanitized_slug: 服务器配置}``。项目行遮蔽同 sanitized slug 的全局行；
    后续项目行覆盖更早的项目行（对应目录文件的重复 slug 规则）。
    """
    if not context.db_session:
        _log.warning("MCP catalog load skipped: no database session on context")
        return {}

    result = await context.db_session.execute(
        text(_MCP_TABLE_SELECT + " WHERE (project_id = :pid OR project_id IS NULL) AND enabled = :enabled"),
        {"pid": context.project_id, "enabled": True},
    )

    global_cfgs: dict[str, dict[str, Any]] = {}
    project_cfgs: dict[str, dict[str, Any]] = {}
    for row in result.fetchall():
        key = sanitize_slug(row.slug)
        if not key:
            continue
        if row.project_id is None:
            global_cfgs.setdefault(key, mcp_row_to_config(row))
        else:
            project_cfgs[key] = mcp_row_to_config(row)

    servers = dict(global_cfgs)
    servers.update(project_cfgs)  # 项目条目遮蔽全局条目
    return servers


def load_mcp_servers_from_dir(
    catalog_path: Path, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """从目录文件加载并索引 MCP 服务器目录。

    返回 ``{sanitized_slug: 服务器配置}``。禁用服务器被跳过，除非设置
    ``include_disabled``（同步用它使禁用条目在表中保留标志，API 用它让界面能显示
    它们）。缺失文件、读取错误和逐块解析失败被记录并跳过——本函数从不抛异常，也从不
    返回 None。

    保持同步，使非智能体调用方（mcp_sync 服务）能复用完全相同的解析器；
    ``load_mcp_servers`` 是面向智能体的表读取器。
    """
    if not catalog_path.exists():
        _log.debug("MCP catalog not found at %s", catalog_path)
        return {}

    try:
        content = catalog_path.read_text("utf-8")
    except OSError as exc:
        _log.warning("MCP catalog read failed: %s", exc)
        return {}

    # 剥离 YAML frontmatter（文件可能有也可能没有）。
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("MCP catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    servers: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("MCP catalog YAML block skipped: %s", exc)
            continue
        if not isinstance(block, dict):
            continue
        raw_servers = block.get("servers")
        if not isinstance(raw_servers, list):
            continue
        for entry in raw_servers:
            if not isinstance(entry, dict) or not entry.get("slug"):
                _log.debug("MCP server entry without slug skipped: %r", entry)
                continue
            slug = sanitize_slug(str(entry["slug"]))
            if not slug:
                continue
            if slug in servers:
                _log.warning("MCP server slug %r duplicated; later entry overrides", slug)
            servers[slug] = entry

    # 就地过滤禁用服务器（除非调用方想列出它们）。
    if not include_disabled:
        disabled = [s for s, cfg in servers.items() if not cfg.get("enabled", True)]
        for s in disabled:
            _log.debug("MCP server %r disabled, skipping", s)
            servers.pop(s, None)

    return servers
