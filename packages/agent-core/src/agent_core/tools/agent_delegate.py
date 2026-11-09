"""委派工具——把子任务交给另一个智能体并取回其结果。

``list_agents`` 显示本项目可委派给谁；``delegate_agent`` 把其中一个作为当前会话的
嵌套轮次运行，并返回它做了什么的摘要。重活（嵌套轮次、事件隔离、并发闸门）位于
``api.services.delegation``，经惰性导入到达——与 ``tools/scheduler.py`` 使用的
接缝相同，因此 agent-core 对 API 包不保留导入期依赖。

两个工具都是可选模块：未声明它们的智能体看不到它们，而在 frontmatter 里声明
``delegates_to`` 的智能体只能到达该名单上的 slug。
"""
from __future__ import annotations


from typing import Any

from ..delegation import list_delegation_candidates
from .base import Tool, ToolContext


def _policy(context: ToolContext) -> tuple[str, ...]:
    """调用方智能体的 ``delegates_to`` 白名单（空 = 不受限）。"""
    delegation = getattr(context, "delegation", None)
    return tuple(getattr(delegation, "policy", ()) or ())


def _chain(context: ToolContext) -> tuple[str, ...]:
    delegation = getattr(context, "delegation", None)
    return tuple(getattr(delegation, "chain", ()) or ())


class ListAgentsTool(Tool):
    """我可以把工作交给谁？"""

    name = "list_agents"
    prompt_hint = (
        "List the agents this project can delegate to. Call it only when you "
        "lack a capability the task needs — not to browse."
    )
    description = (
        "List the agents available for delegation in this project, with what each "
        "one is for (display_name, description, category, skills, workflows). Use "
        "it when the task needs a capability you do not have, then call "
        "delegate_agent with the slug. Optional 'query' filters by substring and "
        "'category' by exact category."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Case-insensitive substring matched against slug, display name, description and skills.",
            },
            "category": {
                "type": "string",
                "description": "Exact category filter (e.g. agile-development, quality).",
            },
        },
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        policy = _policy(context)
        candidates = list_delegation_candidates(
            context.project_fs_path,
            context.framework_root,
            # 智能体自身，以及链条上已在更上游的每个智能体：把子任务往回交入会
            # 循环。
            exclude=_chain(context),
            allow=policy or None,
        )
        records = [c.as_dict() for c in candidates]

        category = str(params.get("category") or "").strip()
        if category:
            records = [r for r in records if r["category"] == category]
        query = str(params.get("query") or "").strip().lower()
        if query:
            records = [
                r for r in records
                if query in " ".join(
                    [r["slug"], r["display_name"], r["description"], *r["skills"]]
                ).lower()
            ]

        if not records:
            return {
                "agents": [],
                "note": (
                    "No agents match. Complete the task yourself, or tell the user "
                    "which capability is missing."
                ),
            }
        return {
            "agents": records,
            "total": len(records),
            "note": (
                "Only delegating to a capability you actually lack is useful: "
                "delegate_agent needs a concrete reason."
            ),
        }


class AgentDelegateTool(Tool):
    """把子任务交给另一个智能体。"""

    name = "delegate_agent"
    prompt_hint = (
        "Hand a subtask to another agent when it needs a capability you do not "
        "have (a tool you lack, a domain you do not know). The agent runs as a "
        "sub-task of this conversation, its reply is visible to the user, and its "
        "summary comes back to you — then you finish the user's answer. Never "
        "delegate work you can do yourself."
    )
    description = (
        "Run another agent on a self-contained subtask and get its result back. "
        "Requirements: 'agent' is a slug from list_agents; 'brief' is the full "
        "instruction for that agent, written as if to a colleague who has not seen "
        "this conversation — include the concrete inputs it needs; 'reason' states "
        "what capability you lack, in one sentence. Returns status (ok / error / "
        "timeout / aborted / refused), a summary of what the agent did, and the "
        "message_id of its reply in the transcript. The agent may ask the user to "
        "confirm a risky action; you do not need to relay it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Target agent slug, exactly as returned by list_agents.",
            },
            "brief": {
                "type": "string",
                "description": (
                    "The subtask, self-contained: what to do, which inputs to use, "
                    "what to return. The agent sees the conversation history but not "
                    "your reasoning about why you are delegating."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One sentence on why you cannot do this yourself (missing tool, "
                    "missing domain knowledge). Shown to the user."
                ),
            },
        },
        "required": ["agent", "brief", "reason"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        agent_slug = str(params.get("agent") or "").strip()
        brief = str(params.get("brief") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if not agent_slug:
            return {"status": "refused", "error": "agent is required. Call list_agents first."}
        if not brief:
            return {"status": "refused", "error": "brief is required - the agent cannot see why you are delegating."}
        if not reason:
            return {"status": "refused", "error": "reason is required - state the capability you lack."}

        try:
            from api.services.delegation import run_delegated_turn
        except ImportError:
            return {"status": "refused", "error": "Delegation is unavailable in this environment."}

        try:
            return await run_delegated_turn(
                context, agent_slug=agent_slug, brief=brief, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - 以文本形式呈现给模型
            # 工具绝不能抛进智能体循环：炸掉的委派是失败的子任务，而非失败的轮次。
            return {
                "status": "error",
                "agent": agent_slug,
                "error": f"{type(exc).__name__}: {exc}",
            }
