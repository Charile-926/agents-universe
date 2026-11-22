"""把当前用户自己的聊天记录与工具调用记录炼化成一个 Skill。

设计约束（与多用户隔离要求一一对应）：

1. **只读当前用户的数据。** 所有查询都以
   ``Conversation.user_id == current_user.user_id`` 为前置条件；前端传来的
   会话 id 一律先做归属校验，不属于当前用户或不可访问时按“不存在”处理
   （404），避免通过 id 猜测探测他人数据。
2. **不泄露内部元数据。** 返回给前端与写进 Skill 的内容只包含会话标题、
   消息文本、工具名/参数键/状态；不含工作区路径、密钥、模型配置、数据库字段。
3. **数据量大时分批。** 会话数、每会话消息数、消息总量都有硬上限，超出部分
   截断并在响应里明确告知，而不是一次性把整库读进内存。
4. **输出是确定性的。** 不依赖外部大模型即可产出可用的 Skill 草稿，便于离线
   测试与复现；生成结果由用户预览后再自行保存/导出。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.errors import api_error
from api.models.conversation import Conversation, Message
from api.models.project import Project

router = APIRouter(prefix="/api/skills/distill", tags=["skills-distill"])

# ── 分批上限 ────────────────────────────────────────────────────────────────
MAX_CONVERSATIONS = 50
MAX_MESSAGES_PER_CONVERSATION = 80
MAX_TOTAL_MESSAGES = 3000
MAX_SNIPPET_CHARS = 400

# 工具调用参数里可能夹带凭证或本机路径，写进 Skill 前统一脱敏。
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    (re.compile(r"[A-Za-z]:\\[^\s\"']+"), "<本地路径>"),
    (re.compile(r"/(?:home|Users|root|var|opt)/[^\s\"']+"), "<本地路径>"),
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"), r"\1=***"),
)


def _redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _snippet(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """单行化 + 截断 + 脱敏，用于把长消息塞进 Skill 草稿。"""
    flat = re.sub(r"\s+", " ", (text or "")).strip()
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return _redact(flat)


def _parse_tool_calls(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


class DistillRequest(BaseModel):
    # 为空 + all_conversations=True 时按“全部（受上限约束）”处理。
    conversation_ids: list[str] = Field(default_factory=list, max_length=MAX_CONVERSATIONS)
    all_conversations: bool = False
    include_tool_calls: bool = True


@router.get("/sources")
async def list_sources(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """当前用户可炼化的会话列表（分页）。

    只返回 ``source IS NULL``（普通侧边栏会话）且属于当前用户的行；发布会话
    （source='publish'）是外部访客产生的，不属于用户的个人工作记录。
    """
    base = (
        select(Conversation, Project.display_name)
        .join(Project, Project.project_id == Conversation.project_id)
        .where(
            Conversation.user_id == current_user.user_id,
            Conversation.status == "active",
            Project.is_active == True,  # noqa: E712
            or_(Conversation.source.is_(None), Conversation.source != "publish"),
        )
    )
    if q:
        needle = f"%{q.strip()}%"
        base = base.where(Conversation.title.like(needle))

    total = await db.scalar(
        select(func.count()).select_from(base.order_by(None).subquery())
    )

    rows = await db.execute(
        base.order_by(
            func.coalesce(Conversation.updated_at, Conversation.created_at).desc()
        )
        .limit(limit)
        .offset(offset)
    )
    items = rows.all()
    if not items:
        return {"items": [], "total": int(total or 0), "limit": limit, "offset": offset}

    conv_ids = [c.conversation_id for c, _ in items]
    # 每条会话的消息数与工具调用数（一次聚合，避免 N+1）。
    stats_rows = await db.execute(
        select(
            Message.conversation_id,
            func.count(Message.message_id),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Message.tool_calls.isnot(None),
                                Message.tool_calls != "",
                                Message.tool_calls != "[]",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .where(Message.conversation_id.in_(conv_ids))
        .group_by(Message.conversation_id)
    )
    stats = {
        cid: (int(msgs or 0), int(tools or 0))
        for cid, msgs, tools in stats_rows.all()
    }

    return {
        "items": [
            {
                "conversation_id": c.conversation_id,
                "title": c.title or "未命名会话",
                "project_name": project_name,
                "updated_at": (c.updated_at or c.created_at).isoformat(),
                "message_count": stats.get(c.conversation_id, (0, 0))[0],
                "tool_call_count": stats.get(c.conversation_id, (0, 0))[1],
            }
            for c, project_name in items
        ],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


async def _load_owned_conversations(
    db: AsyncSession, user_id: str, ids: list[str]
) -> list[Conversation]:
    """按归属加载会话；任何一条不属于当前用户即整体拒绝（404，不泄露存在性）。"""
    unique_ids = list(dict.fromkeys(ids))[:MAX_CONVERSATIONS]
    rows = await db.execute(
        select(Conversation)
        .join(Project, Project.project_id == Conversation.project_id)
        .where(
            Conversation.conversation_id.in_(unique_ids),
            Conversation.user_id == user_id,
            Conversation.status == "active",
            Project.is_active == True,  # noqa: E712
        )
    )
    found = {c.conversation_id: c for c in rows.scalars().all()}
    if len(found) != len(unique_ids):
        raise api_error(
            404,
            "DISTILL_SOURCE_NOT_FOUND",
            "部分会话不存在或无权访问，请刷新列表后重试",
        )
    # 保持请求顺序
    return [found[i] for i in unique_ids]


async def _load_all_owned_conversations(
    db: AsyncSession, user_id: str
) -> tuple[list[Conversation], bool]:
    """最近 N 条会话 + 是否被截断。"""
    rows = await db.execute(
        select(Conversation)
        .join(Project, Project.project_id == Conversation.project_id)
        .where(
            Conversation.user_id == user_id,
            Conversation.status == "active",
            Project.is_active == True,  # noqa: E712
            or_(Conversation.source.is_(None), Conversation.source != "publish"),
        )
        .order_by(func.coalesce(Conversation.updated_at, Conversation.created_at).desc())
        .limit(MAX_CONVERSATIONS + 1)
    )
    items = list(rows.scalars().all())
    truncated = len(items) > MAX_CONVERSATIONS
    return items[:MAX_CONVERSATIONS], truncated


async def _load_messages(
    db: AsyncSession, conversation_id: str
) -> tuple[list[Message], bool]:
    """取一条会话最近的消息（按时间正序返回），并报告是否截断。"""
    rows = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.sequence_num.desc())
        .limit(MAX_MESSAGES_PER_CONVERSATION + 1)
    )
    items = list(rows.scalars().all())
    truncated = len(items) > MAX_MESSAGES_PER_CONVERSATION
    items = items[:MAX_MESSAGES_PER_CONVERSATION]
    items.reverse()
    return items, truncated


def _slug_for(user_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short = re.sub(r"[^a-z0-9]", "", user_id.lower())[:6] or "user"
    return f"distilled-{short}-{stamp}"


def _build_skill(
    conversations: list[Conversation],
    messages_by_conv: dict[str, list[Message]],
    include_tool_calls: bool,
    truncated: bool,
) -> tuple[str, dict]:
    """把会话记录汇总成一份 Skill 草稿（确定性生成，无外部依赖）。"""
    user_id = conversations[0].user_id if conversations else "user"
    total_messages = 0
    total_tool_calls = 0
    tool_counts: dict[str, int] = {}
    tool_errors: dict[str, list[str]] = {}
    tool_arg_keys: dict[str, set[str]] = {}
    transitions: dict[tuple[str, str], int] = {}
    intents: list[str] = []
    outcomes: list[str] = []
    titles: list[str] = []

    for conv in conversations:
        if conv.title:
            titles.append(conv.title)
        msgs = messages_by_conv.get(conv.conversation_id, [])
        total_messages += len(msgs)
        last_sequence: list[str] = []
        for msg in msgs:
            calls = _parse_tool_calls(msg.tool_calls) if include_tool_calls else []
            if calls:
                total_tool_calls += len(calls)
                for call in calls:
                    name = str(call.get("tool") or call.get("name") or "unknown")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    status = str(call.get("status") or "")
                    output = call.get("output")
                    if status == "error" or (isinstance(output, dict) and output.get("error")):
                        detail = ""
                        if isinstance(output, dict) and output.get("error"):
                            detail = _snippet(str(output.get("error")), 160)
                        tool_errors.setdefault(name, []).append(detail or "调用失败")
                    payload = call.get("input")
                    if isinstance(payload, dict):
                        tool_arg_keys.setdefault(name, set()).update(
                            str(k) for k in payload.keys()
                        )
                    if last_sequence and last_sequence[-1] != name:
                        transitions[(last_sequence[-1], name)] = (
                            transitions.get((last_sequence[-1], name), 0) + 1
                        )
                    last_sequence.append(name)
            if msg.role == "user":
                text = _snippet(msg.content, 160)
                if text:
                    intents.append(text)
            elif msg.role == "assistant" and msg.content:
                text = _snippet(msg.content, 160)
                if text and len(outcomes) < 30:
                    outcomes.append(text)

    # 最常见的工具衔接顺序，转成“先 A 再 B”的流程建议。
    ranked_transitions = sorted(
        transitions.items(), key=lambda kv: kv[1], reverse=True
    )[:6]
    ranked_tools = sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)

    triggers = [t for t in dict.fromkeys(titles)][:6]
    trigger_lines = "\n".join(f'  - "{_redact(t)}"' for t in triggers) or '  - "复盘历史会话"'
    tool_list = "\n".join(f"  - {name}" for name, _ in ranked_tools[:12]) or "  - filesystem"

    description = (
        f"从 {len(conversations)} 个会话、{total_messages} 条消息"
        f"{'、' + str(total_tool_calls) + ' 次工具调用' if include_tool_calls else ''}"
        "中提炼出的工作方法；用于复现同类任务的处理步骤与检查要点"
    )

    lines: list[str] = []
    lines.append("---")
    lines.append(f'slug: "{_slug_for(user_id)}"')
    lines.append("description:")
    lines.append(f'  zh-CN: "{description}"')
    lines.append('type: "guidance"')
    lines.append("triggers:")
    lines.append(trigger_lines)
    lines.append("tools:")
    lines.append(tool_list)
    lines.append("---")
    lines.append("")
    lines.append("# Skill：从历史会话提炼的工作方法")
    lines.append("")
    lines.append("> 本文件由“聊天记录炼化”根据当前账号自己的会话与工具调用记录生成，"
                 "仅包含该账号可见的内容。请按实际情况删改后再投入使用。")
    lines.append("")

    lines.append("## 适用场景")
    lines.append("")
    if titles:
        for title in dict.fromkeys(titles[:8]):
            lines.append(f"- {_redact(title)}")
    else:
        lines.append("- 暂无足够的历史会话，建议先完成几次真实任务再炼化。")
    lines.append("")

    lines.append("## 典型需求")
    lines.append("")
    if intents:
        for text in list(dict.fromkeys(intents))[:10]:
            lines.append(f"- {text}")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 标准流程")
    lines.append("")
    if ranked_transitions:
        for idx, ((first, second), count) in enumerate(ranked_transitions, start=1):
            lines.append(f"{idx}. 先执行 `{first}`，再执行 `{second}`（历史出现 {count} 次）。")
    else:
        lines.append("1. 先确认输入与目标，再选择工具；每步完成后核对结果。")
    lines.append(f"{len(ranked_transitions) + 1}. 收尾时复核产物是否齐全，并把结论写回项目知识库。")
    lines.append("")

    if include_tool_calls:
        lines.append("## 工具使用规范")
        lines.append("")
        if ranked_tools:
            lines.append("| 工具 | 调用次数 | 常见参数 |")
            lines.append("| --- | --- | --- |")
            for name, count in ranked_tools[:12]:
                keys = ", ".join(sorted(tool_arg_keys.get(name, set()))[:6]) or "—"
                lines.append(f"| `{name}` | {count} | {keys} |")
        else:
            lines.append("- 历史记录中没有工具调用。")
        lines.append("")

        lines.append("## 已知失败与处理")
        lines.append("")
        if tool_errors:
            for name, errs in list(tool_errors.items())[:8]:
                sample = _snippet(errs[0], 120)
                lines.append(f"- `{name}`：历史失败 {len(errs)} 次，典型原因：{sample}")
        else:
            lines.append("- 历史记录中没有失败的工具调用。")
        lines.append("")

    lines.append("## 产出参考")
    lines.append("")
    if outcomes:
        for text in outcomes[:5]:
            lines.append(f"- {text}")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 质量检查清单")
    lines.append("")
    lines.append("- [ ] 输入数据来源已确认，未混入其他账号或外部系统的数据。")
    lines.append("- [ ] 每一步工具调用的结果都已核对，失败步骤有明确处理方式。")
    lines.append("- [ ] 输出中不含密钥、本机绝对路径、内部接口地址等敏感信息。")
    lines.append("- [ ] 结论与数字可追溯到具体记录，没有凭印象补充。")
    if truncated:
        lines.append("- [ ] 本次炼化触发了分批上限，长会话只取了最近的消息，必要时补看完整记录。")
    lines.append("")

    stats = {
        "conversations": len(conversations),
        "messages": total_messages,
        "tool_calls": total_tool_calls if include_tool_calls else 0,
        "tools": [name for name, _ in ranked_tools],
        "truncated": truncated,
    }
    return "\n".join(lines), stats


@router.post("/generate")
async def generate_skill(
    body: DistillRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """生成 Skill 草稿（预览用）。不写盘，由用户自行保存/导出。"""
    truncated = False
    if body.conversation_ids:
        conversations = await _load_owned_conversations(
            db, current_user.user_id, body.conversation_ids
        )
    elif body.all_conversations:
        conversations, truncated = await _load_all_owned_conversations(
            db, current_user.user_id
        )
    else:
        raise api_error(
            400,
            "DISTILL_NO_SOURCE",
            "请至少选择一个会话，或选择汇总全部会话",
        )
    if not conversations:
        raise api_error(
            404,
            "DISTILL_NO_HISTORY",
            "当前账号还没有可用于炼化的聊天记录",
        )

    messages_by_conv: dict[str, list[Message]] = {}
    loaded = 0
    for conv in conversations:
        if loaded >= MAX_TOTAL_MESSAGES:
            truncated = True
            break
        msgs, cut = await _load_messages(db, conv.conversation_id)
        if cut:
            truncated = True
        room = MAX_TOTAL_MESSAGES - loaded
        if len(msgs) > room:
            msgs = msgs[-room:]
            truncated = True
        messages_by_conv[conv.conversation_id] = msgs
        loaded += len(msgs)

    markdown, stats = _build_skill(
        conversations, messages_by_conv, body.include_tool_calls, truncated
    )
    stats["message_limit_per_conversation"] = MAX_MESSAGES_PER_CONVERSATION
    return {"markdown": markdown, "stats": stats}
