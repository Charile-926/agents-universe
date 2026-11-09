"""技能来源目录加载器——推荐的外部技能仓库。

目录是 Markdown，带一个 YAML 围栏块（见 knowledge/_template/skill-sources.md），
内含 ``sources:`` 列表。与 _catalog.py 相同的宽松约定：格式错误的块被跳过，缺失
文件不产生来源，加载器从不抛异常。

在该文件出现之前创建的项目没有项目目录，因此以框架模板回退——否则内置默认值只会
到达新创建的项目。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from ._catalog import _YAML_FENCE_RE
from ._repo_paths import _REPO_PATH_RE
from .base import ToolContext

_log = logging.getLogger(__name__)

CATALOG_REL_PATH = "knowledge/integrations/skill-sources.md"
TEMPLATE_REL_PATH = "knowledge/_template/skill-sources.md"


def _derive_key(entry: dict[str, Any]) -> str:
    """为未声明 key 的条目尽力推导一个 key。"""
    repo = str(entry.get("repo") or "").strip()
    if repo:
        return repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    url = str(entry.get("url") or "").strip()
    if url:
        return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    return ""


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    repo = str(raw.get("repo") or "").strip() or None
    url = str(raw.get("url") or "").strip() or None
    entry: dict[str, Any] = {
        "key": str(raw.get("key") or "").strip() or _derive_key(raw),
        "repo": repo,
        "url": url,
        "ref": str(raw.get("ref") or "").strip() or None,
        "path": str(raw.get("path") or "").strip().strip("/") or None,
        "notes": str(raw.get("notes") or "").strip(),
        "tags": [str(t) for t in raw.get("tags") or [] if str(t).strip()],
        "valid": True,
        "error": None,
    }
    if not repo and not url:
        entry["valid"] = False
        entry["error"] = "entry needs either 'repo' (owner/name) or 'url'"
    elif any("<" in (value or "") or ">" in (value or "") for value in (repo, url)):
        # 模板用 <owner>/<repo> 占位符说明形态——把它当作真实来源会把智能体引到
        # 伪造主机。
        entry["valid"] = False
        entry["error"] = "placeholder address — replace it with a real repository"
    elif repo and not _REPO_PATH_RE.fullmatch(repo):
        entry["valid"] = False
        entry["error"] = f"invalid repo {repo!r}: expected owner/name"
    if not entry["key"]:
        entry["valid"] = False
        entry["error"] = entry["error"] or "cannot derive a key — set 'key' explicitly"
    return entry


def _iter_entries(block: Any) -> list[Any]:
    """从包装形式或裸形式产出原始条目。"""
    if not isinstance(block, dict):
        return []
    sources = block.get("sources")
    if isinstance(sources, list):
        return sources
    if isinstance(sources, dict):
        # {sources: {key: {...}}}——该 key 同时充当条目 key。
        out = []
        for key, value in sources.items():
            if isinstance(value, dict):
                out.append({"key": key, **value})
        return out
    if block.get("repo") or block.get("url"):
        return [block]
    return []


def _parse_catalog(content: str) -> list[dict[str, Any]]:
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("skill-source catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    sources: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("skill-source catalog YAML block skipped: %s", exc)
            continue
        for raw in _iter_entries(block):
            entry = _normalize_entry(raw)
            if entry is None:
                continue
            key = entry["key"]
            if key in sources:
                _log.warning("skill-source catalog duplicate key %r — later entry wins", key)
            sources[key] = entry
    return list(sources.values())


async def _read_catalog(path: Path) -> list[dict[str, Any]] | None:
    if not await asyncio.to_thread(path.exists):
        return None
    try:
        content = await asyncio.to_thread(path.read_text, "utf-8")
    except OSError as exc:
        _log.warning("skill-source catalog read failed for %s: %s", path, exc)
        return []
    return _parse_catalog(content)


async def load_skill_sources(context: ToolContext) -> dict[str, Any]:
    """解析目录：先项目文件，框架模板作为回退。

    返回 ``{catalog_path, origin, sources}``，其中 ``catalog_path`` 是智能体可交给
    文件系统工具的、相对工作区或框架的路径，``origin`` 为
    ``"project"``/``"template"``/``None``。
    """
    result: dict[str, Any] = {"catalog_path": None, "origin": None, "sources": []}

    project_path = Path(context.knowledge_dir()) / "integrations" / "skill-sources.md"
    sources = await _read_catalog(project_path)
    if sources is not None:
        result["catalog_path"] = CATALOG_REL_PATH
        result["origin"] = "project"
        result["sources"] = sources
        return result

    framework_root = getattr(context, "framework_root", None)
    if framework_root:
        template_path = Path(framework_root) / "knowledge" / "_template" / "skill-sources.md"
        sources = await _read_catalog(template_path)
        if sources is not None:
            result["catalog_path"] = TEMPLATE_REL_PATH
            result["origin"] = "template"
            result["sources"] = sources
            return result

    _log.debug("skill-source catalog not found (project=%s, framework=%s)", project_path, framework_root)
    return result
