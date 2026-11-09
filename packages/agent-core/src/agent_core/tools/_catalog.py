"""集成目录加载器——把 api_request 的 endpoint_key 解析为项目
integrations/custom-api 知识文件中的具体请求默认值。

目录是 Markdown，每个集成一个 YAML 围栏块（见 knowledge/_template/custom-api.md）。
本模块是唯一的服务端读取器，使目录成为端点解析的事实来源；智能体传 endpoint_key，
工具从目录填入方法默认、路径、按环境的 base_url、allowed_hosts、response_json_path
和认证默认。

解析刻意宽松：格式错误的块不会破坏整个目录，缺失/无法读取的文件产生空字典
（调用方决定回退语义）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from .base import ToolContext

_log = logging.getLogger(__name__)

CATALOG_SLUG = "integrations/custom-api"

_YAML_FENCE_RE = re.compile(
    r"^```ya?ml\s*\n(.*?)```", re.DOTALL | re.MULTILINE | re.IGNORECASE
)


def strip_jsonpath_prefix(path: str) -> str:
    """把目录的 response_json_path 规范化为纯点号表示。

    目录模板历史上用类 JSONPath 的 "$.data"；工具的提取器期望 "data"。剥离开头的
    "$." / "." 字符。
    """
    return path.lstrip("$.")


async def load_integration_catalog(context: ToolContext) -> dict[str, dict[str, Any]]:
    """加载并索引项目的集成目录。

    返回 {integration_key: 已解析 YAML 块}。缺失文件、读取错误和逐块解析失败被记录
    并跳过——本函数从不抛异常，也从不返回 None。
    """
    catalog_path = Path(context.knowledge_dir()) / "integrations" / "custom-api.md"
    if not await asyncio.to_thread(catalog_path.exists):
        _log.debug("integration catalog not found at %s", catalog_path)
        return {}

    try:
        content = await asyncio.to_thread(catalog_path.read_text, "utf-8")
    except OSError as exc:
        _log.warning("integration catalog read failed: %s", exc)
        return {}

    # 剥离 YAML frontmatter（文件可能有也可能没有）。
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("integration catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    catalog: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("integration catalog YAML block skipped: %s", exc)
            continue
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("integrations"), dict):
            # 包装形式：{integrations: {key: {...}, ...}}
            for key, entry in block["integrations"].items():
                if isinstance(entry, dict):
                    catalog[str(key)] = entry
        elif isinstance(block.get("integrations"), list):
            # 包装形式：{integrations: [{integration_key: ..., ...}, ...]}
            for entry in block["integrations"]:
                if isinstance(entry, dict) and entry.get("integration_key"):
                    catalog[str(entry["integration_key"])] = entry
        elif block.get("integration_key"):
            catalog[str(block["integration_key"])] = block
        else:
            _log.debug(
                "integration catalog block without integration_key skipped"
            )
    return catalog


def resolve_catalog_endpoint(
    catalog: dict[str, dict[str, Any]],
    integration_key: str,
    endpoint_key: str,
    environment: str | None,
) -> dict[str, Any] | None:
    """在已索引目录中查找端点。

    返回合并视图 {endpoint, environments, auth, defaults, env_name}，集成或端点
    缺失时返回 None。字段级解析（环境选择、优先级）留给调用方。
    """
    config = catalog.get(integration_key)
    if not config or not isinstance(config, dict):
        return None
    # 每个字段都做强制类型转换：目录是手写 YAML，期望对象处出现列表/字符串必须产生
    # “未找到”，而非在调用方 .get() 上抛 AttributeError。
    endpoints = config.get("endpoints")
    if not isinstance(endpoints, dict):
        return None
    endpoint = endpoints.get(endpoint_key)
    if not isinstance(endpoint, dict):
        return None

    def mapping(name: str) -> dict[str, Any]:
        value = config.get(name)
        return value if isinstance(value, dict) else {}

    return {
        "endpoint": endpoint,
        "environments": mapping("environments"),
        "auth": mapping("auth"),
        "defaults": mapping("defaults"),
        "env_name": environment,
    }
