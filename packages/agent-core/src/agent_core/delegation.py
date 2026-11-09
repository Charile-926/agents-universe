"""委派（delegation）所需的 Agent 名单发现。

当一个 Agent 缺少某项能力时，会把子任务交给另一个 Agent。为此它必须先知道
这里都有谁，因此本模块只回答一个问题：*当前项目可以委派给哪些 Agent 定义，
它们各自声称擅长什么？*

名单从文件系统读取，而不是 ``agents`` 表，因为它必须与运行时实际加载的
内容一致：``agent_sync`` 解析 slug 时，先找
``{project_fs_path}/agents/{slug}.agent.md``，再找全局 ``agents/`` 目录，被
委派的轮次也以相同方式重新解析。数据库行可能比其定义文件存活得更久（或
早于惰性的项目同步），那会宣传一个其轮次随后以 ``AGENT_CONFIG_NOT_FOUND``
失败的 Agent。

作用范围是项目自己的 ``agents/`` 目录加上框架级目录——绝不包含兄弟项目。
项目 Agent 必须带 ``{project_slug}--`` 前缀（与 ``agent_sync`` 强制的规则
相同），且 frontmatter 的 ``slug`` 必须等于文件主干，因此我们返回的每个
slug 都能解析回读取它的那个文件。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Iterable

import frontmatter

from .definition_check import project_slug_from_workspace, validate_agent_slug
from .localization import normalize_i18n_field

log = logging.getLogger("agents_universe.agent_core.delegation")

#: Agent 定义文件的后缀。
AGENT_SUFFIX = ".agent.md"


def _as_list(value: object) -> list[str]:
    """把 frontmatter 列表字段归一化为字符串列表。

    由 LLM 编写的定义常把列表写成标量（``delegates_to: "tech-lead"``）；
    这里镜像 ``AgentConfig._list_field``，保证标量永远不会被按字符遍历。
    """
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


@dataclass(frozen=True)
class DelegationCandidate:
    """委派方 Agent 可以交出子任务的一个候选 Agent。"""

    slug: str
    display_name: str
    description: str
    category: str
    skills: tuple[str, ...]
    workflows: tuple[str, ...]
    definition_path: str

    def as_dict(self) -> dict[str, Any]:
        """JSON 可序列化形式（工具结果以 JSON 编码）。"""
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "skills": list(self.skills),
            "workflows": list(self.workflows),
            "definition_path": self.definition_path,
        }


def _load_candidate(path: Path, *, require_prefix: str | None) -> DelegationCandidate | None:
    """解析一个定义文件；当它不应被宣传时返回 None。

    以下情况跳过（仅记录日志，镜像 ``agent_sync.sync_agents_dir``）：
    frontmatter 无法解析的文件、``slug`` 缺失/不安全、slug 与文件主干不
    匹配，或（在项目目录中）slug 缺少 ``{project_slug}--`` 前缀。
    """
    try:
        post = frontmatter.load(str(path))
    except Exception:
        log.warning("Delegation roster: unparsable agent definition %s", path)
        return None

    slug = post.get("slug")
    if not slug or not validate_agent_slug(slug):
        log.warning("Delegation roster: %s has missing/unsafe slug %r", path, slug)
        return None
    if slug != path.name.removesuffix(AGENT_SUFFIX):
        # 运行时按 `{slug}.agent.md` 解析，因此不匹配的主干会宣传一个无法
        # 加载的 slug。
        log.warning("Delegation roster: %s declares slug %r, skipped", path, slug)
        return None
    if require_prefix and not slug.startswith(require_prefix):
        log.warning(
            "Delegation roster: project agent %r in %s lacks required prefix %r, skipped",
            slug, path, require_prefix,
        )
        return None

    # 两者都遵循中文优先规范：名单会喂给面向 LLM 的工具结果——多语言映射
    # 必须折叠为数据库投影（以及 i18n 之前的 str()）会显示的同一段中文，
    # 而绝不能是 str(dict)。
    display_name, _ = normalize_i18n_field(
        post.get("display_name"), canonical_order=("zh-CN", "en-US")
    )
    description, _ = normalize_i18n_field(
        post.get("description"), canonical_order=("zh-CN", "en-US")
    )

    return DelegationCandidate(
        slug=slug,
        display_name=display_name or slug,
        description=description or "",
        category=str(post.get("category") or "agile-development"),
        skills=tuple(_as_list(post.get("skills"))),
        workflows=tuple(_as_list(post.get("workflows"))),
        definition_path=str(path),
    )


def list_delegation_candidates(
    project_fs_path: str | None,
    framework_root: str | None,
    *,
    exclude: Iterable[str] = (),
    allow: Collection[str] | None = None,
) -> list[DelegationCandidate]:
    """返回当前项目可以委派给的 Agent。

    项目定义会覆盖同 slug 的框架定义。``exclude`` 移除不能调用的 slug
    （调用方自身，以及委派链更上层已在运行的任何 Agent）；``allow`` 非空
    时，把结果限制在该允许列表内（Agent 定义中可选的 ``delegates_to:``
    frontmatter；``None`` 或空表示整个名单）。

    ``framework_root`` 是持有全局 ``agents/`` 目录的仓库根目录
    （``ToolContext.framework_root``）。传入 ``None`` 仅列出项目 Agent——
    这是在 Web 服务之外运行 agent-core 时正确的降级行为。
    """
    blocked = set(exclude)
    allowed = {a for a in allow} if allow else None

    found: dict[str, DelegationCandidate] = {}

    # 先读框架目录，使同 slug 的项目定义最终胜出。（项目 slug 带前缀，真正
    # 冲突的可能性不大——但无论如何，覆盖规则都必须与
    # resolve_agent_definition_path 一致。）
    if framework_root:
        global_dir = Path(framework_root) / "agents"
        for md_file in sorted(global_dir.glob(f"*{AGENT_SUFFIX}")):
            candidate = _load_candidate(md_file, require_prefix=None)
            if candidate:
                found[candidate.slug] = candidate

    if project_fs_path:
        project_dir = Path(project_fs_path) / "agents"
        project_slug = project_slug_from_workspace(project_fs_path)
        prefix = f"{project_slug}--" if project_slug else None
        for md_file in sorted(project_dir.glob(f"*{AGENT_SUFFIX}")):
            candidate = _load_candidate(md_file, require_prefix=prefix)
            if candidate:
                found[candidate.slug] = candidate

    return sorted(
        (
            c
            for slug, c in found.items()
            if slug not in blocked and (allowed is None or slug in allowed)
        ),
        key=lambda c: (c.category, c.display_name, c.slug),
    )


def read_delegation_policy(definition_path: str | None) -> tuple[str, ...]:
    """返回 Agent 定义声明的 ``delegates_to`` 允许列表。

    空元组表示"无限制——整个名单"，无法读取或缺失的文件也会得到同样
    结果：不能把读不到的策略，误认为是禁止一切的策略。
    """
    if not definition_path:
        return ()
    try:
        if not Path(definition_path).is_file():
            return ()
        post = frontmatter.load(definition_path)
    except Exception:
        log.warning("Delegation policy: unparsable definition %s", definition_path)
        return ()
    return tuple(_as_list(post.get("delegates_to")))
