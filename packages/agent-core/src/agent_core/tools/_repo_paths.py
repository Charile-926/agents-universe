"""git_repo 与 repo_graph 工具共享的仓库路径解析。

git_repo 保持其原有行为；repo_graph 复用同一校验，使两个工具对“什么是检出、它
位于何处”达成一致（并阻塞相同的路径穿越尝试）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 与之前相同的名称约束：克隆目标和检出名称。
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def extract_repo_name(repository: str) -> str:
    name = repository.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def repos_dir(project_fs_path: str) -> Path:
    """工作区检出根目录：``{project}/repos``。"""
    return Path(project_fs_path).resolve() / "repos"


def list_clones(project_fs_path: str) -> list[str]:
    """工作区内的检出名称（带 .git 条目的目录）。"""
    directory = repos_dir(project_fs_path)
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    )


def resolve_repo_path(
    params: dict[str, Any],
    project_fs_path: str,
    *,
    available: list[str] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """把 'repository' 或 'repository_path' 解析为绝对检出路径。

    成功返回（路径, None），否则返回（None, 错误）。两个参数都未给且 ``available``
    恰好只有一个克隆时，使用该克隆（模型通常忘记命名仓库）；否则错误列出选项。
    ``available=None`` 完全跳过回退。
    """
    repository = params.get("repository")
    repository_path = params.get("repository_path")
    if bool(repository) == bool(repository_path):
        if repository_path:
            return None, {
                "error": "Exactly one of 'repository' or 'repository_path' is required"
            }
        if available is not None and len(available) == 1:
            # 走被守卫的分支：唯一克隆仍须通过名称和包含检查（repos/ 里的符号链接
            # 条目会作为克隆列出，却解析到工作区之外）。
            return resolve_repo_path(
                {"repository": available[0]}, project_fs_path, available=None
            )
        error: dict[str, Any] = {
            "error": "Exactly one of 'repository' or 'repository_path' is required"
        }
        error["available_repos"] = available or []
        if available:
            error["hint"] = (
                "Retry with 'repository' set to one of: "
                + ", ".join(available) + "."
            )
        else:
            error["hint"] = (
                "Clone a repository first, or call list_repos to enumerate clones."
            )
        return None, error

    base = Path(project_fs_path).resolve()
    if repository_path:
        candidate = str(repository_path).replace("\\", "/")
        raw = Path(candidate)
        if raw.is_absolute() or candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
            return None, {"error": "'repository_path' must be a project-relative path"}
        resolved = (base / raw).resolve()
        if not resolved.is_relative_to(base):
            return None, {"error": "Path traversal blocked"}
        return resolved, None

    name = extract_repo_name(str(repository))
    # "." / ".." 匹配名称正则，却解析到工作区根（repos/.. == 项目自身）——
    # 如果工作区是 git 仓库，每个操作都会静默指向它而非克隆的仓库。
    if name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        return None, {"error": f"Invalid repository name: {name!r}"}
    resolved = (base / "repos" / name).resolve()
    # 与上方 repository_path 和 git_repo._op_clone 相同的包含规则：repos/<name>
    # 处的符号链接解析到工作区之外，每个 git 操作（pull/commit/push）随后都会读写
    # 那个外来检出。
    if not resolved.is_relative_to(base):
        return None, {"error": "Path traversal blocked"}
    return resolved, None
