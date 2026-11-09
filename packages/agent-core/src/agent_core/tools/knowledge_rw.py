"""知识读写工具——管理项目中的 Markdown 知识文件。

支持带动态加载/卸载的分层知识：
- read：一次性文件访问（内容仅在工具结果中）；`slugs` 一次读取多个
- write：把变更持久化到磁盘
- load：把详情文件带入持久动态上下文
- unload：释放动态加载的文件
- refresh：重读已在上下文中的文件（静态或动态）
- status：显示上下文状态（静态、动态、延迟、溢出）
- list：列出带状态标记的文件
- search_by_slug：slug 部分匹配
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

import frontmatter

from ..knowledge.loader import (
    MAX_FILE_SIZE,
    _is_log_slug,
    derive_summary,
    dynamic_budget_error,
    normalize_children,
)
from ..paths import KNOWLEDGE_SLUG_RE, PathEscapeError, resolve_within
from .base import Tool, ToolContext

_log = logging.getLogger("agent_core.knowledge_rw")

_SLUG_RE = KNOWLEDGE_SLUG_RE

# 追加到每个“非法 slug”错误之后：slug 正则按设计只接受 ASCII（路径穿越防御），
# 而 CJK 命名文件是常见错误。
_SLUG_HINT = (
    " Slugs must be ASCII path segments; for a Chinese name keep the ASCII "
    'slug/filename and put the name in frontmatter `title` (e.g. title: "API 详情").'
)

# 写入以 UTF-8 字节计，上限略低于 loader.MAX_FILE_SIZE（512 KB），使写入的文件
# 总能加载回上下文。基于字符的上限会在 CJK 密集内容（每字符 3 字节）上超出加载器
# 字节限制，文件会以“overflow”静默掉出上下文。
_MAX_WRITE_BYTES = 500 * 1024


async def _read_knowledge_file(path: Path) -> str:
    """读取知识文件，执行加载器 512 KB 上限。

    loader.py 只索引不超过 MAX_FILE_SIZE 的文件；在这里把更大的文件整体读进 LLM
    上下文，会在加载器本会拒绝它的地方撑爆上下文预算。
    """
    stat = path.stat()
    if stat.st_size > MAX_FILE_SIZE:
        raise ValueError(
            f"Knowledge file {path.name} is {stat.st_size} bytes — above the "
            f"{MAX_FILE_SIZE} byte load limit. Split the file or lower its "
            f"knowledge_level."
        )
    # UTF-8 BOM 会粘在内容开头并使哈希/解析偏移。
    return (await asyncio.to_thread(path.read_text, "utf-8")).lstrip("\ufeff")


def _safe_resolve(slug: str, knowledge_dir: Path) -> Path | None:
    """把 slug 解析为 knowledge_dir 内的路径。slug 格式非法或穿越时返回 None。"""
    if not slug or not _SLUG_RE.match(slug):
        return None
    try:
        return resolve_within(knowledge_dir, f"{slug}.md")
    except PathEscapeError:
        return None


class KnowledgeRWTool(Tool):
    name = "knowledge_rw"
    prompt_hint = (
        "Read/write/delete project knowledge Markdown and manage what is loaded into context; "
        "writes and deletes through this tool keep the knowledge index in sync, so prefer it over "
        "filesystem for knowledge files. Before 'write', use 'list' to see the project's existing "
        "files and prefer updating an existing file — especially an empty template file with "
        "'(to be filled …)' placeholders — over creating a new slug. Use 'load' for files needed "
        "across turns, 'read' for one-off lookups."
    )
    description = (
        "Read, write, delete, load, unload, refresh, or list Markdown knowledge files. "
        "Use 'load' to bring detail files into persistent context (visible across turns). "
        "Use 'read' for one-time access. Use 'unload' to release dynamic context. "
        "Use 'refresh' to reload a file that was updated. "
        "Use 'children' to list immediate children of a knowledge file. "
        "Use 'delete' to remove a knowledge file and its database index row together. "
        "Use 'purge' to clean up database index rows whose files no longer exist "
        "(optional slug purges a single row; without slug purges all residue for the project). "
        "Slug format: '{category}/{filename-without-extension}'. "
        "Before 'write', check 'list' for an existing file that covers the content and update "
        "it instead of creating a new slug."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "delete", "purge", "load", "unload", "refresh", "status", "list", "search_by_slug", "children"],
            },
            "slug": {
                "type": "string",
                "description": "Knowledge slug, e.g. 'domain/context' or 'technical/api/get-users'",
            },
            "slugs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Several slugs for read — one call instead of one per file. "
                    "Files that are not 'detail' level are already in context; "
                    "only read what you actually still need."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full Markdown content including frontmatter (for write only)",
            },
            "change_summary": {
                "type": "string",
                "description": "Brief description of what changed (for write only)",
            },
            "category": {
                "type": "string",
                "description": "Filter by category for list operation",
            },
            "root_only": {
                "type": "boolean",
                "description": "Only list root-level files (no parent) for list operation",
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        # 只解析一次：每个操作都把该路径与 rglob/resolve 产出的路径（已完全解析）
        # 配对，而含符号链接或 `..` 段的 knowledge_dir 会让 relative_to() 抛
        # ValueError——整个 `list`/`read`/`write` 调用失败而非返回文件。
        knowledge_dir = Path(context.knowledge_dir()).resolve()

        if operation == "list":
            return await self._op_list(params, knowledge_dir, context)

        elif operation == "search_by_slug":
            return await self._op_search(params, knowledge_dir)

        elif operation == "read":
            if params.get("slugs") and not params.get("slug"):
                return await self._op_read_many(params, knowledge_dir)
            return await self._op_read(params, knowledge_dir)

        elif operation == "write":
            return await self._op_write(params, knowledge_dir, context)

        elif operation == "load":
            return await self._op_load(params, knowledge_dir, context)

        elif operation == "unload":
            return await self._op_unload(params, context)

        elif operation == "refresh":
            return await self._op_refresh(params, knowledge_dir, context)

        elif operation == "status":
            return self._op_status(context)

        elif operation == "children":
            return await self._op_children(params, context)

        elif operation == "delete":
            return await self._op_delete(params, knowledge_dir, context)

        elif operation == "purge":
            return await self._op_purge(params, context)

        return {"error": f"Unknown operation: {operation}"}

    # ------------------------------------------------------------------
    # 各操作实现
    # ------------------------------------------------------------------

    async def _op_list(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        category = params.get("category")
        root_only = params.get("root_only", False)
        base = knowledge_dir.resolve()
        search_root = (knowledge_dir / category).resolve() if category else base
        if not search_root.is_relative_to(base):
            return {"error": f"Invalid category: {category!r}"}

        def _scan():
            files = []
            if not search_root.exists():
                return files
            for md_file in sorted(search_root.rglob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                rel = md_file.relative_to(knowledge_dir)
                slug = str(rel.with_suffix("")).replace("\\", "/")
                try:
                    post = frontmatter.load(str(md_file))
                    title = post.metadata.get("title", md_file.stem)
                    summary = post.metadata.get("summary", "")
                    level = post.metadata.get("knowledge_level", "auto")
                    parent = post.metadata.get("parent", None)
                    children = post.metadata.get("children", [])
                    if not summary:
                        summary = derive_summary(post.content or "")
                except Exception:
                    title = md_file.stem
                    summary = ""
                    level = "auto"
                    parent = None
                    children = []
                files.append({
                    "slug": slug,
                    "title": title,
                    "knowledge_level": level,
                    "summary": summary,
                    "parent_slug": parent,
                    "children_count": len(normalize_children(children)),
                    # CJK/非法 slug 无法通过本工具读写——标记它，使智能体重命名
                    # 而非重试。
                    **({} if _SLUG_RE.match(slug) else {"status": "invalid_slug"}),
                })
            return files

        files = await asyncio.to_thread(_scan)
        if root_only:
            files = [f for f in files if not f["parent_slug"]]
        for f in files:
            if f.get("status") == "invalid_slug":
                continue  # keep the rename signal; context status is meaningless for it
            f["status"] = self._get_slug_status(f["slug"], context)
        return {"files": files, "count": len(files)}

    async def _op_search(self, params: dict, knowledge_dir: Path) -> dict:
        slug = params.get("slug", "")

        def _scan():
            results = []
            if not knowledge_dir.exists():
                return results
            for md_file in knowledge_dir.rglob("*.md"):
                rel_slug = str(md_file.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
                if slug.lower() in rel_slug.lower():
                    results.append(rel_slug)
            return results

        results = await asyncio.to_thread(_scan)
        return {"matches": results}

    async def _op_read(self, params: dict, knowledge_dir: Path) -> dict:
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for read operation"}
        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}.{_SLUG_HINT}"}
        import logging
        _log = logging.getLogger("agent_core.knowledge_rw")
        # 只记录 slug——避免把主机文件系统结构泄漏进日志
        _log.info("knowledge_rw read: slug=%s", slug)
        if not await asyncio.to_thread(file_path.exists):
            kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
            suggestions = []
            if kdir_exists:
                def _suggest() -> list[str]:
                    found = []
                    for md in knowledge_dir.rglob("*.md"):
                        rel = str(md.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
                        if rel == slug or rel.endswith(f"/{slug}"):
                            found.append(rel)
                    return found
                suggestions = await asyncio.to_thread(_suggest)
            err: dict = {
                "error": f"Knowledge file not found: {slug}",
                "knowledge_dir_exists": kdir_exists,
            }
            if not kdir_exists:
                err["hint"] = (
                    f"Knowledge directory does not exist: {knowledge_dir}. "
                    f"Check that PROJECTS_ROOT is configured correctly and the "
                    f"project workspace was initialized on disk."
                )
            if suggestions:
                err["did_you_mean"] = suggestions
            return err
        try:
            content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}
        post = frontmatter.loads(content)
        return {
            "slug": slug,
            "content": content,
            "metadata": dict(post.metadata),
            "word_count": len(post.content.split()),
        }

    async def _op_read_many(self, params: dict, knowledge_dir: Path) -> dict:
        """一次调用读取多个 slug。

        答案保持调用方顺序并携带逐文件错误（缺失 slug 报告自身，不拖垮整批）——
        与 confluence 批量读取相同的约定。
        """
        slugs = params.get("slugs") or []
        # LLM 字符串化的参数：裸字符串会逐字符迭代。
        if isinstance(slugs, str):
            slugs = slugs.split(",")
        # 两个分支都做 strip——纯空白条目是调用方本无意索要的 slug。
        slugs = [s.strip() for s in slugs if isinstance(s, str) and s.strip()]
        if not slugs:
            return {"error": "slugs must be a non-empty array of slugs"}

        results = await asyncio.gather(
            *[self._op_read({"slug": s}, knowledge_dir) for s in slugs],
            return_exceptions=True,
        )
        files = []
        for slug, result in zip(slugs, results):
            if isinstance(result, BaseException):
                files.append({"slug": slug, "error": str(result)})
            else:
                files.append(result)
        return {"files": files, "count": len(files)}

    async def _op_write(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        slug = params.get("slug")
        content = params.get("content", "")
        change_summary = params.get("change_summary", "Updated")

        if not slug:
            return {"error": "slug is required for write operation"}
        if not _SLUG_RE.match(slug):
            return {"error": f"Invalid slug format: {slug!r}. Use ASCII path-safe segments (letters, digits, '-', '_', '.') joined by slashes." + _SLUG_HINT}
        # loader.MAX_FILE_SIZE（512 KB）会把超大文件静默移出上下文——把写入
        # （以 UTF-8 字节计）限制在其下，使智能体写入的内容总能再次加载。
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_WRITE_BYTES:
            return {"error": (
                f"Content is too large ({content_bytes} bytes > {_MAX_WRITE_BYTES}). "
                "Split it into smaller files (e.g. an index plus detail files "
                "with knowledge_level: detail)."
            )}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            # _safe_resolve 也拒绝其文件已作为指向知识目录之外符号链接存在的
            # slug（resolve_within 走真实路径）——写那里会覆盖外部文件。
            return {"error": f"Invalid or escaping slug: {slug!r}"}

        # slug 的文件在磁盘上尚不存在时为“新”——DB 索引行只在本次写入后由
        # reindex_one 创建，因此磁盘是“项目是否已有此文件”的权威检查。
        is_new_file = not await asyncio.to_thread(file_path.exists)

        def _do_write() -> dict:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            if file_path.exists():
                existing_hash = hashlib.sha256(file_path.read_text(encoding="utf-8").encode()).hexdigest()
                if existing_hash == content_hash:
                    return {"success": True, "slug": slug, "changed": False, "reason": "content identical",
                            "content_hash": content_hash}
            file_path.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "slug": slug,
                "path": str(file_path),
                "word_count": len(content.split()),
                "content_hash": content_hash,
                "change_summary": change_summary,
                "changed": True,
            }

        result = await asyncio.to_thread(_do_write)
        result["indexed"] = False
        if result.get("changed"):
            # 先做层级闭合：子文件 frontmatter 的 `parent` 必须在子文件 reindex
            # 运行之前落入父文件的 `children`，使单次写入流程让两行保持一致。
            try:
                meta = frontmatter.loads(content).metadata
            except Exception:
                meta = {}
            parent_slug = meta.get("parent")
            if parent_slug:
                try:
                    from agent_core.knowledge.index import sync_parent_children
                    synced = await sync_parent_children(
                        child_slug=slug,
                        parent_slug=str(parent_slug),
                        project_id=context.project_id,
                        db_session=context.db_session,
                        knowledge_dir=knowledge_dir,
                    )
                    if synced and context.project_context is not None:
                        # 让内存中父条目的 children 列表同步，使 children/status
                        # 无需重载即可回答。
                        pctx = context.project_context
                        pentry = pctx.deferred_entries.get(str(parent_slug)) or next(
                            (e for e in pctx.loaded_entries if e.slug == str(parent_slug)),
                            None,
                        )
                        if pentry is not None and slug not in pentry.children_slugs:
                            pentry.children_slugs.append(slug)
                except Exception:
                    _log.warning(
                        "Parent children sync failed: child=%s parent=%s project=%s",
                        slug, parent_slug, context.project_id, exc_info=True,
                    )
                    result.setdefault(
                        "warnings", [],
                    ).append(f"Could not add {slug} to parent {parent_slug}'s children list.")

        # 每次写入都 reindex，包括内容未变的写入：磁盘哈希匹配只证明文件当前，不
        # 证明知识行存在或与其匹配（之前的 reindex 可能失败，或数据库已重建）。
        # indexed=True 必须意味着行确实被刷新，因此幂等写入会修复索引。
        if context.db_session is not None:
            try:
                from agent_core.knowledge.index import reindex_one
                reindex_result = await reindex_one(
                    fs_path=str(file_path),
                    project_id=context.project_id,
                    db_session=context.db_session,
                    knowledge_root=knowledge_dir,
                )
                if reindex_result.get("error"):
                    raise RuntimeError(reindex_result["error"])
                result["indexed"] = True
                for w in reindex_result.get("warnings") or []:
                    result.setdefault("warnings", []).append(w)
            except Exception:
                _log.warning("Knowledge reindex failed for slug=%s project=%s", slug, context.project_id, exc_info=True)
                # 无索引行的详情文件对延迟列表不可见——失败必须可见，不能静默。
                result.setdefault("warnings", []).append(
                    f"File written but knowledge index NOT updated for {slug} "
                    "(reindex failed). Detail files missing an index row are "
                    "invisible to the deferred list — run "
                    'knowledge_rw(operation="write") again or reindex the project.'
                )
        else:
            result.setdefault("warnings", []).append(
                f"File written but knowledge index NOT updated for {slug} "
                "(no database session). Detail files missing an index row "
                "are invisible to the deferred list."
            )
        # 完全失效：缓存的条目列表是下一次会话喂给 deferred_entries/status 的
        # 内容（缓存的逐文件内容从不填充）——否则 reindex 的文件会持续提供陈旧
        # 元数据（title/summary/word_count）直到进程重启。对应 _op_delete。
        if result["indexed"] and context.knowledge_cache is not None:
            context.knowledge_cache.invalidate(context.project_id)
        # 通知前端，使知识进度条实时更新。修复了缺失/陈旧行的未变写入也改变了
        # 索引，因此同样发出事件。
        if context.session is not None and (result.get("changed") or result["indexed"]):
            try:
                await context.session.emit("knowledge_updated", slug=slug)
            except Exception:
                _log.debug("knowledge_updated emit failed for slug=%s", slug, exc_info=True)

        if result.get("changed"):
            # 主 API 文件过大且无详情子文件时给出警告
            result = self._check_split_warning(slug, content, knowledge_dir, result)

            # 写入创建全新 slug 时软性推动“先填现有文件”。仅建议——文件仍保持
            # 已写入并已索引。
            if is_new_file:
                result = self._check_placement_note(slug, content, knowledge_dir, result, context)

        return result

    async def _op_delete(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        """Delete a knowledge file and its DB metadata row together."""
        if context.project_id is None:
            return {"error": "No project context — refusing to delete knowledge"}
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for delete operation"}
        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}.{_SLUG_HINT}"}

        # 解除链接之前先读父级，使父级的 children 列表能与写入侧同步对称地清理。
        parent_slug: str | None = None
        if await asyncio.to_thread(file_path.exists):
            try:
                parent_slug = frontmatter.loads(
                    await _read_knowledge_file(file_path)
                ).metadata.get("parent") or None
            except Exception:
                parent_slug = None

        # 删除文件；文件缺失不是错误——DB 行可能仍陈旧，需要在下方清理。
        file_existed = await asyncio.to_thread(file_path.exists)
        if file_existed:
            await asyncio.to_thread(file_path.unlink)

        # 对称层级清理：从父级 children 列表移除该 slug（尽力而为，绝不阻塞删除）。
        if parent_slug:
            try:
                from agent_core.knowledge.index import sync_parent_children
                await sync_parent_children(
                    child_slug=slug,
                    parent_slug=str(parent_slug),
                    project_id=context.project_id,
                    db_session=context.db_session,
                    knowledge_dir=knowledge_dir,
                    add=False,
                )
            except Exception:
                _log.warning(
                    "Parent children cleanup failed: child=%s parent=%s project=%s",
                    slug, parent_slug, context.project_id, exc_info=True,
                )

        # 尽力而为的 DB 行删除（绝不阻塞文件删除的成功）。
        db_action = "skipped"
        if context.db_session is not None:
            try:
                from agent_core.knowledge.index import delete_one

                result = await delete_one(slug, context.project_id, context.db_session)
                db_action = result.get("action", "error")
            except Exception:
                _log.warning("Knowledge delete failed for slug=%s project=%s", slug, context.project_id, exc_info=True)

        # 从内存会话上下文移除条目，使 status/children/deferred 立即反映删除。
        ctx = context.project_context
        if ctx is not None:
            ctx.deferred_entries.pop(slug, None)
            ctx.loaded_content.pop(slug, None)
            ctx.overflow_slugs = [s for s in ctx.overflow_slugs if s != slug]
            ctx.overflow_entries.pop(slug, None)
            from agent_core.knowledge.loader import unload_dynamic_entry

            unload_dynamic_entry(ctx, slug)

        # 完全失效：陈旧条目列表是下次喂给 deferred_entries 的内容（缓存的逐文件
        # 内容从不填充，因此逐 slug 驱逐会是空操作）。
        if context.knowledge_cache is not None:
            context.knowledge_cache.invalidate(context.project_id)

        if context.session is not None:
            try:
                await context.session.emit("knowledge_updated", slug=slug)
            except Exception:
                _log.debug("knowledge_updated emit failed for slug=%s", slug, exc_info=True)

        return {"success": True, "slug": slug, "file_deleted": file_existed, "db_row": db_action}

    async def _op_purge(self, params: dict, context: ToolContext) -> dict:
        """只删除陈旧的 knowledge_metadata 行（不改文件）。

        带 slug：删除那一行 DB 行（文件状态无关）。不带 slug：清除项目中文件在
        磁盘上已不存在的每一行——清理早期文件删除留下的残留。
        """
        if context.project_id is None:
            return {"error": "No project context — refusing to purge knowledge"}
        if context.db_session is None:
            return {"error": "No database session available"}

        slug = params.get("slug")
        try:
            if slug:
                if not _SLUG_RE.match(slug):
                    return {"error": f"Invalid slug: {slug!r}"}
                from agent_core.knowledge.index import delete_one

                result = await delete_one(slug, context.project_id, context.db_session)
                deleted = result.get("action") == "deleted"
            else:
                from agent_core.knowledge.index import purge_residue

                result = await purge_residue(context.project_id, context.db_session)
                deleted = result.get("count", 0) > 0
        except Exception:
            _log.warning("Knowledge purge failed for project=%s slug=%s", context.project_id, slug, exc_info=True)
            return {"error": f"Purge failed: {slug or 'all'}"}

        if deleted:
            if context.knowledge_cache is not None:
                context.knowledge_cache.invalidate(context.project_id)
            if context.session is not None:
                try:
                    await context.session.emit("knowledge_updated", slug=slug or "")
                except Exception:
                    _log.debug("knowledge_updated emit failed after purge project=%s", context.project_id, exc_info=True)

        return result

    async def _record_load_event(
        self, context: ToolContext, slug: str, event_type: str, reason: str
    ) -> bool:
        """持久化加载/卸载事件，使加载在上下文重建后仍存活。

        事件行写入时返回 True。失败不致命——内存加载本轮仍工作；只是下一轮不会
        重新水合。外键要求已存在 knowledge_metadata 行，因此无索引行的 slug 记录
        警告并跳过（自洽：未索引 slug 本就无法经 deferred 发现）。
        """
        if context.db_session is None or not context.conversation_id:
            return False
        try:
            from sqlalchemy import text

            row = (await context.db_session.execute(
                text(
                    # 全局行遮蔽同一 slug 时项目行优先（项目优先；.first() 取
                    # 第一行——加载器那里相同的 CASE 折叠进字典，最后即项目的条目
                    # 优先）。
                    "SELECT knowledge_id FROM knowledge_metadata "
                    "WHERE slug = :slug AND (project_id = :pid OR project_id IS NULL) "
                    "ORDER BY CASE WHEN project_id IS NULL THEN 1 ELSE 0 END"
                ),
                {"slug": slug, "pid": context.project_id},
            )).first()
            if row is None:
                _log.warning(
                    "load event skipped: no knowledge_metadata row for slug=%s "
                    "(run knowledge_rw write/reindex so the entry is indexed)",
                    slug,
                )
                return False
            from api.models.knowledge import KnowledgeLoadEvent

            context.db_session.add(KnowledgeLoadEvent(
                knowledge_id=str(row[0]),
                conversation_id=context.conversation_id,
                event_type=event_type,
                reason=reason,
                turn_number=context.current_turn or 0,
            ))
            await context.db_session.commit()
            return True
        except Exception:
            _log.warning("Failed to record %s event for slug=%s", event_type, slug, exc_info=True)
            return False

    async def _op_load(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        from agent_core.knowledge.loader import load_dynamic_entry

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for load operation"}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}.{_SLUG_HINT}"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        # 已在静态上下文
        if slug in ctx.loaded_content:
            return {"status": "already_loaded", "slug": slug, "source": "static"}

        # 已动态加载
        if slug in ctx.dynamically_loaded:
            return {"status": "already_loaded", "slug": slug, "source": "dynamic"}
        if not await asyncio.to_thread(file_path.exists):
            # 全局（project_id 为 NULL）详情条目位于框架知识目录而非项目目录——
            # 延迟列表经 load 承诺它们，因此放弃前先回退到那里。
            fallback = None
            if context.framework_root and ctx.deferred_entries.get(slug) is not None:
                entry = ctx.deferred_entries[slug]
                if entry.project_id is None:
                    fw_candidate = Path(context.framework_root) / "knowledge" / f"{slug}.md"
                    if await asyncio.to_thread(fw_candidate.exists):
                        fallback = fw_candidate
            if fallback is None:
                kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
                err: dict = {
                    "error": f"Knowledge file not found: {slug}",
                    "knowledge_dir_exists": kdir_exists,
                }
                if not kdir_exists:
                    err["hint"] = (
                        f"Knowledge directory does not exist: {knowledge_dir}. "
                        f"Check PROJECTS_ROOT configuration."
                    )
                return err
            file_path = fallback

        try:
            content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}

        # 入口处的预算闸门：动态区域没有降级循环，因此无界的加载序列会撑爆请求
        # 大小。
        budget = dynamic_budget_error(ctx, len(content.encode("utf-8")))
        if budget:
            return {"error": budget, "slug": slug}

        # 加载之前先捕获延迟条目——load_dynamic_entry 会把它从 deferred_entries
        # 弹出，而模型希望加载结果里带摘要（否则它是盲加载）。
        entry = ctx.deferred_entries.get(slug)

        # 加载进动态上下文。不绑定当前任务：加载必须在显式 unload 之前挺过
        # plan_task 管道和轮次边界（事件每轮重新水合）。
        load_dynamic_entry(ctx, slug, content, context.current_turn, task_id=None)

        await self._record_load_event(context, slug, "load", "agent_request")

        # Web 界面监听该事件以填充动态知识面板。
        if context.session is not None:
            try:
                await context.session.emit("knowledge_dynamic_load", slug=slug)
            except Exception:
                _log.debug("knowledge_dynamic_load emit failed for slug=%s", slug, exc_info=True)

        return {
            "status": "loaded",
            "slug": slug,
            "summary": entry.summary if entry else "",
            "bytes": len(content.encode("utf-8")),
        }

    async def _op_unload(self, params: dict, context: ToolContext) -> dict:
        from agent_core.knowledge.loader import unload_dynamic_entry

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for unload operation"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        removed = unload_dynamic_entry(ctx, slug)
        if not removed:
            return {"status": "not_loaded", "slug": slug}

        await self._record_load_event(context, slug, "unload", "manual")

        if context.session is not None:
            try:
                await context.session.emit(
                    "knowledge_dynamic_unload", slugs=[slug], reason="manual"
                )
            except Exception:
                _log.debug("knowledge_dynamic_unload emit failed for slug=%s", slug, exc_info=True)
        return {"status": "unloaded", "slug": slug}

    async def _op_refresh(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        """重读已在上下文（静态或动态）中的文件以获取变更。"""
        from agent_core.knowledge.loader import refresh_dynamic_entry, update_context_file

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for refresh operation"}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}
        if not await asyncio.to_thread(file_path.exists):
            kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
            err: dict = {
                "error": f"Knowledge file not found: {slug}",
                "knowledge_dir_exists": kdir_exists,
            }
            if not kdir_exists:
                err["hint"] = (
                    f"Knowledge directory does not exist: {knowledge_dir}. "
                    f"Check PROJECTS_ROOT configuration."
                )
            return err

        try:
            new_content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}

        if slug in ctx.dynamically_loaded:
            refresh_dynamic_entry(ctx, slug, new_content)
            return {"status": "refreshed", "slug": slug, "source": "dynamic"}
        elif slug in ctx.loaded_content:
            update_context_file(ctx, slug, new_content)
            return {"status": "refreshed", "slug": slug, "source": "static"}
        else:
            return {"status": "not_loaded", "slug": slug, "hint": "Use 'load' to bring this file into context first."}

    def _op_status(self, context: ToolContext) -> dict:
        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        return {
            "static_loaded": len(ctx.loaded_content),
            "dynamically_loaded": [
                {"slug": s, "bound_to_task": r.task_id}
                for s, r in ctx.dynamic_records.items()
                if s in ctx.dynamically_loaded
            ],
            "dynamic_count": len(ctx.dynamically_loaded),
            "deferred_available": [
                {"slug": e.slug, "summary": e.summary, "parent": e.parent_slug, "depth": e.depth}
                for e in ctx.deferred_entries.values()
            ],
            "overflow": ctx.overflow_slugs,
        }

    async def _op_children(self, params: dict, context: ToolContext) -> dict:
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for children operation"}

        ctx = context.project_context
        children = []
        seen: set[str] = set()

        def _add(entry) -> None:
            if entry.slug in seen:
                return
            seen.add(entry.slug)
            children.append({
                "slug": entry.slug,
                "title": entry.title,
                "summary": entry.summary,
                "depth": entry.depth,
                "has_children": bool(entry.children_slugs),
            })

        # 第一层：内存会话上下文（延迟 + 静态）。
        if ctx is not None:
            for entry in list(ctx.deferred_entries.values()) + ctx.loaded_entries:
                if entry.parent_slug == slug:
                    _add(entry)

        # 第二层：数据库——捕获在其他会话加载的子文件，或条目从未进入本上下文的
        # 文件。
        if context.db_session is not None:
            try:
                from sqlalchemy import text

                # 项目行优先：_add() 先到先得，项目文件遮蔽同 slug 的全局文件
                # （对应加载器的折叠）。没有这个顺序，胜者是数据库先返回的任意
                # 条目。
                rows = (await context.db_session.execute(
                    text(
                        "SELECT slug, title, summary, depth, children_slugs, fs_path "
                        "FROM knowledge_metadata "
                        "WHERE parent_slug = :pid_slug "
                        "AND (project_id = :cur OR project_id IS NULL) "
                        "AND is_archived = :archived "
                        "ORDER BY CASE WHEN project_id IS NULL THEN 1 ELSE 0 END"
                    ),
                    {"pid_slug": slug, "cur": context.project_id, "archived": False},
                )).mappings().all()
                import json as _json

                for row in rows:
                    from ..knowledge.loader import KnowledgeEntry
                    _add(KnowledgeEntry(
                        knowledge_id="",
                        slug=row["slug"],
                        title=row["title"] or row["slug"],
                        fs_path=row["fs_path"] or "",
                        category=(row["slug"] or "").split("/", 1)[0],
                        cross_references=[],
                        word_count=0,
                        knowledge_level="detail",
                        parent_slug=slug,
                        children_slugs=_json.loads(row["children_slugs"] or "[]"),
                        summary=row["summary"] or "",
                        depth=int(row["depth"] or 0),
                    ))
            except Exception:
                _log.warning("children DB lookup failed for slug=%s", slug, exc_info=True)

        # 第三层：磁盘——索引行缺失时 frontmatter 的 `parent` 是事实来源
        # （reindex 失败绝不能隐藏子文件）。
        knowledge_dir = Path(context.knowledge_dir()).resolve()

        def _disk_scan() -> list:
            found = []
            if not knowledge_dir.exists():
                return found
            for md_file in knowledge_dir.rglob("*.md"):
                try:
                    post = frontmatter.load(str(md_file))
                except Exception:
                    continue
                if (post.metadata.get("parent") or None) != slug:
                    continue
                rel = str(md_file.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
                found.append((rel, post))
            return found

        for rel, post in await asyncio.to_thread(_disk_scan):
            if rel in seen:
                continue
            from ..knowledge.loader import derive_summary

            meta = post.metadata
            seen.add(rel)
            children.append({
                "slug": rel,
                "title": meta.get("title") or rel.rsplit("/", 1)[-1],
                "summary": str(meta.get("summary") or derive_summary(post.content or "")),
                "depth": 0,
                "has_children": bool(meta.get("children")),
            })

        children.sort(key=lambda c: c["slug"])
        return {"parent_slug": slug, "children": children, "count": len(children)}

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    _SPLIT_REQUIRED_SLUGS = ("technical/api-map", "technical/kong-map")
    _SPLIT_WORD_THRESHOLD = 2000
    _SPLIT_CHILD_DIRS = {
        "technical/api-map": "technical/api",
        "technical/kong-map": "technical/kong",
    }

    def _check_split_warning(self, slug: str, content: str, knowledge_dir: Path, result: dict) -> dict:
        """主 API 文件超过词数阈值且无详情子文件时追加警告。"""
        if slug not in self._SPLIT_REQUIRED_SLUGS:
            return result
        word_count = len(content.split())
        if word_count < self._SPLIT_WORD_THRESHOLD:
            return result

        child_dir = knowledge_dir / self._SPLIT_CHILD_DIRS[slug]
        has_children = child_dir.exists() and any(child_dir.glob("*.md"))
        if has_children:
            return result

        result["warning"] = (
            f"MANDATORY SPLIT REQUIRED: '{slug}' has {word_count} words but NO detail child files exist. "
            f"You MUST create per-service detail files under '{self._SPLIT_CHILD_DIRS[slug]}/' with "
            f"knowledge_level: detail and parent: \"{slug}\". "
            f"The task is NOT complete until detail files are created. "
            f"See knowledge-manager skill 'API Documentation: Mandatory Two-Level Structure' for format."
        )
        return result

    _PLACEMENT_NOTE_EXEMPT_PREFIXES = ("technical/api/", "technical/kong/")
    _NOTE_SLUG_LIMIT = 30

    def _check_placement_note(
        self, slug: str, content: str, knowledge_dir: Path, result: dict, context: ToolContext
    ) -> dict:
        """写入创建了全新 slug 时追加建议性提示。

        软性推动“先填现有文件”，但不阻塞合法新文件：强制两级 API/Kong 详情文件和
        层级子文件（frontmatter `parent`）豁免。
        """
        if slug.startswith(self._PLACEMENT_NOTE_EXEMPT_PREFIXES):
            return result
        try:
            meta = frontmatter.loads(content).metadata
        except Exception:
            meta = {}
        if meta.get("parent"):  # 现有索引的合法层级子文件
            return result
        existing = self._existing_slugs(slug, knowledge_dir, context)
        if not existing:
            return result
        result["note"] = (
            f"Created NEW knowledge file '{slug}'. The project already has these knowledge files: "
            + ", ".join(f"'{s}'" for s in existing)
            + ". If this content fits one of them (including empty template files with "
            "'(to be filled …)' placeholders), merge it into that file and delete this new one "
            'with knowledge_rw(operation="delete", slug="' + slug + '"). Create a new file only '
            "when no existing file covers the content."
        )
        return result

    def _existing_slugs(self, written_slug: str, knowledge_dir: Path, context: ToolContext) -> list[str]:
        """智能体在创建新文件前本应考虑的 slug。

        优先用上下文内条目（即提示词在 '## Project Knowledge'/'Additional
        Knowledge' 下显示的内容），回退到磁盘扫描。排除刚写入的文件。
        """
        ctx = context.project_context
        if ctx is not None:
            slugs = set(ctx.loaded_content) | set(ctx.overflow_slugs) | set(ctx.deferred_entries)
            slugs.discard(written_slug)
            if slugs:
                return sorted(slugs)[: self._NOTE_SLUG_LIMIT]
        if not knowledge_dir.exists():
            return []
        return sorted(
            str(p.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
            for p in knowledge_dir.rglob("*.md")
            if not any(part.startswith("_") for part in p.relative_to(knowledge_dir).parts)
            and str(p.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/") != written_slug
        )[: self._NOTE_SLUG_LIMIT]

    def _get_slug_status(self, slug: str, context: ToolContext) -> str:
        ctx = context.project_context
        if ctx is None:
            return "unknown"
        if slug in ctx.loaded_content:
            return "loaded"
        if slug in ctx.dynamically_loaded:
            return "dynamic"
        if _is_log_slug(slug):
            # 加载器把它排除在 deferred_entries 之外——说明原因而非报告
            # "unindexed"（索引行存在）。
            return "log"
        if slug in ctx.deferred_entries:
            return "deferred"
        if slug in ctx.overflow_slugs:
            return "overflow"
        return "unindexed"
