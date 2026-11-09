"""定义文件（智能体、技能、工作流）的建议性校验。

定义文件是带 YAML 前置元数据的 Markdown。注册是惰性且静默的：``agent_sync``
会跳过前置元数据无法解析、``slug`` 缺失/不安全、或 slug 缺少
``{project_slug}--`` 前缀的项目智能体——只写日志、不建数据库行，因此该智能体
根本不会出现在选择器里。技能/工作流加载器则较宽松，会退而使用文件名主干。

写出文件的大模型看不到这些，因此文件系统工具在写入/读取时运行这些检查并把结果
返回。写入仍会成功——预期调用方修复报告的错误后重写。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import frontmatter

from .localization import is_locale_map, normalize_i18n_field

#: 项目智能体 slug 中项目 slug 与智能体名之间的分隔符。
PROJECT_SLUG_SEPARATOR = "--"

#: 路径安全的 slug 段：小写字母、数字、连字符，必须以字母或数字开头。
AGENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: slug 开头的 ``{name}--``——在重新加上真实项目 slug 前缀之前先剥离，使从
#: 其他项目复制来的定义不会出现双前缀（``proj-a--proj-b--helper``）。
_LEADING_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*--")

_AGENT_SUFFIX = ".agent.md"
_WORKFLOW_SUFFIX = ".workflow.md"


def validate_agent_slug(slug: object) -> bool:
    """当 slug 可安全用作文件名段时返回 True。"""
    return isinstance(slug, str) and bool(AGENT_SLUG_RE.match(slug))


def project_slug_from_workspace(project_fs_path: str | None) -> str | None:
    """返回工作区路径中编码的项目 slug（若有）。

    根项目的工作区是 ``PROJECTS_ROOT/{slug}``，子项目是
    ``PROJECTS_ROOT/{parent}/projects/{child}``，因此目录名就是项目 slug（API
    构建所需智能体 slug 前缀时用的是同样推导）。
    """
    if not project_fs_path:
        return None
    name = Path(project_fs_path).name
    return name if validate_agent_slug(name) else None


def _norm(rel_path: str) -> str:
    """分隔符规范化的、相对工作区的路径。

    会丢弃 ``.`` 段：模型会写出 ``./agents/foo.agent.md`` 之类 shell 风格路径，
    pathlib 能解析到正确文件，但下方的位置/slug 检查是比较字符串——保留
    ``./`` 前缀曾让每个位置正确的定义看起来放错了地方。
    """
    parts = rel_path.replace("\\", "/").lstrip("/").split("/")
    return "/".join(p for p in parts if p not in ("", "."))


def _classify(rel_path: str, scope: str) -> str | None:
    """返回 'agent' | 'skill' | 'workflow'，非定义路径返回 None。"""
    parts = [p for p in _norm(rel_path).split("/") if p not in ("", ".")]
    if not parts:
        return None
    # 加载器会跳过私有/mixin 路径——不会注册任何定义，因此校验它们只会产生噪声。
    if any(part.startswith("_") for part in parts):
        return None
    name = parts[-1]
    if name.endswith(_AGENT_SUFFIX):
        return "agent"
    if parts[0] in ("skills", "workflows") and name.endswith(".md"):
        return parts[0][:-1]
    # 项目工作区里，agents/skills/ 是被复制到错误位置的框架布局——绝不会作为
    # 项目技能加载，因此值得标记。全局作用域读取同一路径则是合法的。
    if scope == "project" and len(parts) > 1 and parts[0] == "agents" and parts[1] == "skills":
        return "skill"
    return None


def _parse(content: str) -> tuple[dict[str, Any], str | None]:
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), None
    except Exception as e:  # noqa: BLE001 —— 任何解析错误都可作为文本报告
        # PyYAML 会把出错行单独换行；压平它，使消息在单行工具结果里仍可读。
        return {}, f"{type(e).__name__}: {' '.join(str(e).split())}"


def _list_field(value: object) -> list[str]:
    """把前置元数据列表字段规范化为字符串列表。

    镜像 ``AgentConfig._list_field``（agent.py）——那个模块会拉入整个工具树，
    在这里导入会循环依赖。行为刻意保持一致：逗号分隔的标量会被拆分，而不是
    逐字符迭代。
    """
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _known_tool_names() -> frozenset[str]:
    from .tools.registry import known_static_tool_names

    return known_static_tool_names()


def _quoted(value: str) -> str:
    """为 YAML 给标量加引号——JSON 字符串是合法的 YAML 双引号标量。"""
    return json.dumps(value, ensure_ascii=False)


def _reference_warnings(
    meta: dict[str, Any],
    *,
    project_fs_path: str | None,
    framework_root: str | None,
) -> list[str]:
    """对解析不到任何文件的技能/工作流引用发出警告。"""
    if not project_fs_path and not framework_root:
        return []
    project = Path(project_fs_path) if project_fs_path else None
    framework = Path(framework_root) if framework_root else None
    warnings: list[str] = []

    for ref in _list_field(meta.get("skills")):
        candidates = []
        if project is not None:
            candidates.append(project / "skills" / f"{ref}.md")
        if framework is not None:
            candidates.append(framework / "agents" / "skills" / f"{ref}.md")
        if not any(c.is_file() for c in candidates):
            warnings.append(f"skills 引用 {ref!r} 找不到对应文件（项目 skills/{ref}.md 或框架 agents/skills/{ref}.md）")

    for ref in _list_field(meta.get("workflows")):
        candidates = []
        if project is not None:
            candidates.append(project / "workflows" / f"{ref}.workflow.md")
        if framework is not None:
            candidates.append(framework / "workflows" / f"{ref}.workflow.md")
        if not any(c.is_file() for c in candidates):
            warnings.append(f"workflows 引用 {ref!r} 找不到对应文件（workflows/{ref}.workflow.md）")

    return warnings


def _tool_warnings(meta: dict[str, Any]) -> list[str]:
    declared = _list_field(meta.get("tools"))
    if not declared:
        return []
    # MCP 标记不是注册表里的工具——它们在运行时从项目的 MCP 目录解析，因此
    # 始终视为已知。
    unknown = [
        t for t in declared
        if t != "mcp" and not t.startswith("mcp:") and t not in _known_tool_names()
    ]
    if not unknown:
        return []
    return [f"tools 声明了 registry 中不存在的工具：{', '.join(unknown)}（运行时会静默丢弃）"]


def _suggested_frontmatter(kind: str, slug: str | None, meta: dict[str, Any]) -> str:
    """调用方可直接粘回并补全的最小可用前置元数据。"""
    slug_line = _quoted(slug) if slug else _quoted("<必填：小写字母/数字/连字符>")
    # 把语言映射折叠为其规范条目——建议是标量模板；原始字典会在 YAML 里渲染成
    # Python repr。
    display, _ = normalize_i18n_field(meta.get("display_name"))
    description, _ = normalize_i18n_field(meta.get("description"))
    lines = ["---", f"slug: {slug_line}"]
    if kind == "agent":
        lines.append(f"display_name: {_quoted(display) if isinstance(display, str) and display else _quoted('<显示名>')}")
        lines.append(f"description: {_quoted(description) if isinstance(description, str) and description else _quoted('<一句话职责>')}")
        lines.append("tools:")
        lines.append("  - filesystem")
        lines.append("skills: []")
        lines.append("workflows: []")
    else:
        lines.append(f"description: {_quoted(description) if isinstance(description, str) and description else _quoted('<一句话说明>')}")
        if kind == "skill":
            lines.append("type: \"guidance\"")
        lines.append("triggers: []")
    lines.append("---")
    return "\n".join(lines)


def _slugify(value: str) -> str:
    """尽力把一个值改写为允许的 slug 字符集。"""
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized if normalized and normalized[0].isalnum() else ""


def _agent_expected_slug(stem: str, slug: str, project_slug: str | None) -> tuple[str | None, str | None]:
    """为智能体定义返回（期望 slug, 必需前缀）。

    前置元数据 slug 可用时从它推导建议，否则从文件名主干推导，因此即使 slug
    错误也能给出一个可写入的具体值。
    """
    prefix = f"{project_slug}{PROJECT_SLUG_SEPARATOR}" if project_slug else None
    for candidate in (slug, stem):
        if not candidate:
            continue
        if prefix and candidate.startswith(prefix):
            return candidate, prefix
        base = _slugify(_LEADING_PREFIX_RE.sub("", candidate))
        if base:
            return (f"{prefix}{base}" if prefix else base), prefix
    return None, prefix


def _check_agent_placement(rel: str, name: str) -> str | None:
    if rel == f"agents/{name}":
        return None
    if rel.startswith("agents/"):
        return f"定义文件必须直接放在 agents/ 下（当前 {rel}）：同步只扫描 agents/*.agent.md，子目录不会被注册。"
    return f"文件不在 agents/ 目录下（当前 {rel}）：不会被注册为智能体。"


def check_definition(
    rel_path: str,
    content: str,
    *,
    scope: str = "project",
    project_slug: str | None = None,
    project_fs_path: str | None = None,
    framework_root: str | None = None,
) -> dict[str, Any] | None:
    """校验一个写入/读取的定义文件；路径不是定义文件时返回 None。

    ``scope`` 对工作区覆盖层为 ``"project"``，对框架目录为 ``"global"``——
    全局定义从不带项目前缀，因此前缀规则只适用于项目作用域。
    """
    kind = _classify(rel_path, scope)
    if kind is None:
        return None

    rel = _norm(rel_path)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    name = parts[-1]
    meta, parse_error = _parse(content)
    errors: list[str] = []
    warnings: list[str] = []

    if parse_error:
        errors.append(
            f"frontmatter YAML 解析失败：{parse_error}；"
            "值里含英文冒号或 # 时必须加引号（如 description: \"...\"）。"
        )

    raw_slug = meta.get("slug")
    slug = raw_slug if isinstance(raw_slug, str) else ""
    expected: dict[str, Any] = {}

    if kind == "agent":
        stem = name[: -len(_AGENT_SUFFIX)]
        expected_slug, prefix = _agent_expected_slug(stem, slug, project_slug if scope == "project" else None)
        if prefix:
            expected["project_slug"] = project_slug
            expected["slug_prefix"] = prefix
        if expected_slug:
            expected["slug"] = expected_slug
            expected["path"] = f"agents/{expected_slug}{_AGENT_SUFFIX}"
        placement_error = _check_agent_placement(rel, name)
        if placement_error:
            errors.append(placement_error)
        # 下方所有内容都读取已解析的前置元数据。解析失败后字段就是未知的，
        # 同时报告“缺少 slug”/“缺少 display_name”只会重述症状，把调用方引向
        # 错误的修复方向。
        if not parse_error:
            if not slug:
                errors.append("frontmatter 缺少 slug 键：没有 slug 的智能体定义不会被注册。")
            else:
                if not validate_agent_slug(slug):
                    errors.append(
                        f"slug {slug!r} 非法：只允许小写字母、数字、连字符，且以字母或数字开头"
                        "（大写、下划线、点、中文都会被拒绝）。"
                    )
                if slug != stem:
                    errors.append(
                        f"slug {slug!r} 与文件名主干 {stem!r} 不一致：运行期按 agents/{{slug}}.agent.md 查找，"
                        "不一致会出现「选择器可见但执行失败」——请改名文件或改 slug，两者必须相同。"
                    )
                if prefix and not slug.startswith(prefix):
                    errors.append(
                        f"项目智能体 slug 必须以 {prefix!r} 开头（当前 {slug!r}）："
                        f"缺少该前缀的定义不会被注册，正确写法示例 {expected_slug!r}。"
                    )
            if not meta.get("display_name"):
                warnings.append("缺少 display_name：选择器里会退化成 slug 显示。")
            for field in ("tools", "skills", "workflows", "starter_prompts"):
                if isinstance(meta.get(field), str):
                    warnings.append(f"{field} 写成了字符串标量：建议用 YAML 列表，避免解析歧义。")
        warnings.extend(_tool_warnings(meta))

    elif kind == "skill":
        expected_slug = rel[len("skills/"): -len(".md")] if rel.startswith("skills/") else ""
        if not rel.startswith("skills/"):
            errors.append(
                f"项目技能必须写到 skills/ 下（当前 {rel}）：agents/skills/ 是框架目录布局，项目不会加载它。"
            )
        if expected_slug:
            expected["slug"] = expected_slug
        if not parse_error:
            if not slug:
                warnings.append(
                    f"缺少 slug：会以文件名主干 {Path(name).stem!r} 注册"
                    f"（约定值为 {expected_slug!r}）。"
                )
            elif expected_slug and slug != expected_slug:
                warnings.append(f"slug {slug!r} 与约定值 {expected_slug!r}（相对 skills/ 的路径）不一致。")
        warnings.extend(_tool_warnings(meta))

    else:  # workflow
        rest = rel[len("workflows/"):] if rel.startswith("workflows/") else rel
        if rest.endswith(_WORKFLOW_SUFFIX):
            expected_slug = rest[: -len(_WORKFLOW_SUFFIX)]
        elif rest.endswith(".md"):
            expected_slug = rest[: -len(".md")]
        else:
            expected_slug = ""
        if expected_slug:
            expected["slug"] = expected_slug
        if not name.endswith(_WORKFLOW_SUFFIX):
            warnings.append(
                f"文件名 {name!r} 不以 {_WORKFLOW_SUFFIX} 结尾（约定 workflows/<slug>.workflow.md）。"
            )
        if not parse_error:
            if not slug:
                hint = f"（约定值为 {expected_slug!r}）" if expected_slug else ""
                warnings.append(
                    f"缺少 slug：loader 会以 {Path(name).stem!r} 注册{hint}，"
                    "与 agent 的 workflows 引用和 /命令 都对不上。"
                )
            elif expected_slug and slug != expected_slug:
                warnings.append(f"slug {slug!r} 与约定值 {expected_slug!r} 不一致。")
        warnings.extend(_tool_warnings(meta))

    warnings.extend(
        _reference_warnings(meta, project_fs_path=project_fs_path, framework_root=framework_root)
    )

    result: dict[str, Any] = {
        "ok": not errors,
        "kind": kind,
        "scope": scope,
        "path": rel_path,
        "slug": slug or None,
        "errors": errors,
        "warnings": warnings,
    }
    if expected:
        result["expected"] = expected
    if errors:
        result["suggested_frontmatter"] = _suggested_frontmatter(kind, expected.get("slug"), meta)
    return result
