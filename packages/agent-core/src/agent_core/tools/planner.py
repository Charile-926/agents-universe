"""规划器工具——为复杂目标生成结构化任务清单。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .base import Tool, ToolContext


class PlannerTool(Tool):
    """生成结构化任务清单。agent-core 主循环拦截该工具调用以切换到任务模式——
    它不调用外部服务。"""

    name = "plan_task"
    prompt_hint = (
        "Required before multi-step or implementation work: break the goal into small "
        "tasks, each with a concrete verification method. Skip it for simple Q&A or "
        "single-step requests."
    )
    description = (
        "Break a complex goal into an ordered list of small, independently verifiable tasks. "
        "Call this when the user's request involves 3 or more distinct actions, "
        "multiple tools, sequential steps, or any implementation work. Each task must "
        "state a concrete verification method before execution and should produce one "
        "small, reviewable change set."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The high-level goal to decompose into tasks",
            },
            "tasks": {
                "type": "array",
                "description": "The structured task list",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "tools_needed": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "estimated_complexity": {
                            "type": "string",
                            "enum": ["low", "mid", "high"],
                        },
                    },
                    "required": ["id", "title", "tools_needed", "depends_on", "estimated_complexity"],
                },
            },
        },
        "required": ["goal", "tasks"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        tasks = params.get("tasks", [])
        # 模式声明的是对象数组，但模型也可能发出裸字符串或字符串列表；对这些调用
        # .get() 会抛 AttributeError，以不透明的崩溃而非模型可纠正的信息到达模型。
        if not isinstance(tasks, list):
            return {"error": "tasks must be an array of task objects"}
        for task in tasks:
            if not isinstance(task, dict):
                return {"error": f"each task must be an object, got {type(task).__name__}"}
        # 未提供 ID 时分配稳定 ID
        for task in tasks:
            if not task.get("id"):
                task["id"] = f"t{uuid.uuid4().hex[:6]}"
        return {
            "accepted": True,
            "task_count": len(tasks),
            "tasks": tasks,
        }
