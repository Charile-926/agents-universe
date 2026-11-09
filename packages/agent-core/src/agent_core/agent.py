"""
Agent 编排器（agent.py）—— 工具调用与智能体主循环。

本模块是 agent-core 引擎的核心，负责把"用户消息 + 系统提示词 + 可用工具"
组织成一次完整的大模型（LLM）对话，并驱动"模型思考 → 决定调用工具 →
执行工具 → 把结果回灌模型"的智能体循环（agentic loop），直到模型给出最终
答复，或触发迭代预算、上下文超限、任务超时等安全边界。

主要由以下几部分组成：
- 模块级辅助函数：JSON 安全序列化（_json_default/_dumps）、凭证脱敏
  （_scrub_credentials）、单任务超时读取（_task_timeout_seconds）、工具
  输出裁剪（_clip_tool_output）、Token 用量记账（_consume_usage）、多模态
  用户消息组装（build_user_content）等；
- AgentConfig：Agent 的运行配置数据类，负责从文件读取并解析各类开关、
  模型/工具白名单、超时与预算等参数；
- Agent：主编排器类，维护系统提示词、工具注册表与模型供应方，对外暴露
  run() 入口；内部通过 _run_loop（普通对话循环）以及 _run_task_mode /
  _execute_single_task / _run_task_loop（多任务规划模式）执行具体流程，
  并统一处理上下文压缩、限流降级、任务进度事件推送等横切逻辑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes length={len(obj)}>"
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    # 兜底处理：MCP 注入的工具可能返回任意对象。这里用文本渲染保证本轮
    # 不中断，避免 TypeError 逃逸到工具循环的 try/except 之外（_dumps 的
    # 调用位于该 try/except 外）。
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)

import frontmatter

from .localization import normalize_i18n_field
from .compressor import (
    MAX_OUTPUT_RESERVE,
    MAX_REQUEST_BYTES,
    SUMMARY_MARKER,
    compress_history,
    compression_budget,
    estimate_history_tokens,
    estimate_request_bytes,
    force_compress_history,
    request_byte_breakdown,
)
from .knowledge.loader import KnowledgeContextResult
from .model_routing import resolve_tier_config
from .providers.base import LLMProvider, Message, StopReason, ToolDefinition, classify_api_error
from .providers.registry import get_provider
from .session import ConversationSession, UserInputEntry
from .skills.registry import SkillRegistry
from .tools._http import redact_secret
from .tools.base import Tool, ToolContext
from .tools.registry import build_tool_registry

_log = logging.getLogger("agent_core.agent")


def _scrub_credentials(text: str, credentials: dict[str, Any] | None) -> str:
    """在文本进入任何输出端之前，掩码掉其中所有已配置的 API Key。

    模型供应方/SDK 抛出的异常往往会回显被拒内容——h11 会把整个
    Authorization 头原样引用（"Illegal header value b'...'"），鉴权错误会
    引用密钥本身——而下方的错误边界会把该字符串发送到前端界面、日志以及
    （通过 task_failed 事件）数据库。这里对原始字节与转义字节都做替换，
    规则与 token/api-key 路由里的 ``_redact_key`` 保持一致。
    """
    if not text or not credentials:
        return text
    for creds in credentials.values():
        if isinstance(creds, dict):
            text = redact_secret(text, creds.get("api_key"))
    return text


def _task_timeout_seconds() -> int:
    """单个任务的执行时间预算（秒），由环境变量 AGENT_TASK_TIMEOUT_SECONDS 配置。

    在调用时才读取，便于部署侧不改代码即可调整。此前硬编码的 300 秒会在
    慢思考模型迭代中途将其切断，并连带跳过所有依赖该任务的后续任务。
    """
    raw = os.environ.get("AGENT_TASK_TIMEOUT_SECONDS", "")
    try:
        value = int(raw) if raw else 600
    except ValueError:
        _log.warning("AGENT_TASK_TIMEOUT_SECONDS is not an integer, using default")
        return 600
    return max(1, value)

# 流式累积的工具调用参数（字符数）上限，防止失控的数据流导致内存无限增长。
_MAX_TOOL_ARGS_CHARS = 2_000_000

# tool_call_end 事件 WebSocket 帧的载荷上限：整个结果会作为一个 JSON 帧
# 发送，而 manager.send 在 5 秒后会回收连接——数 MB 的工具结果（如仓库
# 图谱的大范围输出、读取大文件）会导致客户端被断开。回灌给 LLM 的工具
# 消息仍保留完整结果，这里只限制浏览器实际收到的内容。
_MAX_TOOL_OUTPUT_CHARS = 100_000


def _clip_tool_output(result):
    """返回工具结果的 JSON 安全副本，超限时进行尺寸裁剪。

    外部的特殊单元/对象类型（SQL 结果行里的 Decimal、bytes、Path、set 等）
    必须在此处统一强制转换——这是每次 tool_call_end 事件发出前都经过的唯一
    咽喉点：否则原始 dict 会在 WebSocket 转发和 tool_calls 持久化两处进入
    API 的 json.dumps，一旦抛出 TypeError 就会踢掉活动连接并丢失整条助手
    消息。回灌给 LLM 的工具消息保留原始对象，本函数只塑造作为事件发出的内容。
    """
    if not isinstance(result, (dict, list)):
        return result
    try:
        text = json.dumps(result, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError):
        return result
    if len(text) <= _MAX_TOOL_OUTPUT_CHARS:
        # JSON 往返：上面的强制转换都变成了普通 JSON 类型，因此下游的每次
        # dumps（严格模式或仅日期的 default）都能成功。
        return json.loads(text)
    return {
        "truncated": True,
        "bytes": len(text),
        "preview": text[:50_000],
    }

# 工具调用循环预算（迭代次数 = 模型供应方往返次数）。对话循环在用户运行中
# 插入消息时会刷新其单轮预算；total_budget（5 倍）是永不刷新的安全阀。
# 长闭环工作流（如 QA 的 Jira 流水线）需要比旧的 20/15 更多的迭代次数。
_MAX_CHAT_ITERATIONS = 60
_MAX_TASK_ITERATIONS = 30


def _attachment_ref(a: dict) -> str:
    """为 LLM 生成附件的文本表示（附件名 + 相对路径）。"""
    return (
        f"Attachment: {a.get('name', 'unknown')} ({a.get('media_type', '')}, "
        f"{a.get('size', 0)} bytes) — file path: {a.get('rel_path', '')}"
    )


def _consume_usage(
    usage: dict[str, Any], prev: tuple[int, int]
) -> tuple[int, int, int, int]:
    """把模型供应方的一次用量快照拆分为台账增量与累计总量。

    用量数据块携带的是本次供应方调用自请求开始的累计总量，OpenAI 兼容
    网关会在普通数据块上重复携带这些值（如 vLLM 的运行统计）。若把每个
    快照直接相加，会导致每个数据块都重复计算一次提示词；``prev`` 记录本次
    调用已入账的最大快照，因此增量取此后的增长部分。返回
    ``(提示词增量, 补全增量, 提示词总量, 补全总量)``。
    """
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    return (
        max(0, prompt - prev[0]),
        max(0, completion - prev[1]),
        prompt,
        completion,
    )


def build_user_content(user_message: str, attachments: list[dict] | None, supports_vision: bool) -> str | list:
    """构建发送给 LLM 的用户消息内容。

    无附件时原样返回字符串。有附件时，内容变为多模态分片列表：先是文本
    分片（原始消息、内联文本附件、路径引用），随后是图片分片——这是三个
    供应方适配器都接受的约定。图片仅在供应方支持视觉时作为图像块发送；
    二进制文件和超大图片会降级为路径引用，以便 Agent 用工具去读取它们。
    """
    if not attachments:
        return user_message
    parts: list[dict] = []
    if user_message:
        parts.append({"type": "text", "text": user_message})
    for a in attachments:
        if a.get("inline_text"):
            parts.append({"type": "text", "text": f"### Attachment: {a['name']}\n{a['inline_text']}"})
        elif a.get("image_data") and supports_vision:
            parts.append({"type": "image", "media_type": a["image_media_type"], "data": a["image_data"]})
        else:
            parts.append({"type": "text", "text": _attachment_ref(a)})
    return parts


@dataclass
class AgentConfig:
    """Agent 运行配置数据类。

    封装一个 Agent 定义文件（Markdown 正文 + frontmatter 元数据）解析后的
    全部参数：标识与描述、系统提示词正文，以及工具/技能/工作流/知识库
    白名单、Token 上限与预算、api_request 是否免确认等开关。通过
    from_file() 从磁盘上的 Agent 定义文件构造。
    """
    slug: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    max_tokens: int = 128000
    token_budget: int = 100000
    # 为本 Agent 关闭 api_request 的写操作/生产环境确认闸门（用于 QA 等
    # 需要执行数据准备类调用的自动化 Agent）。
    api_request_no_confirm: bool = False

    @staticmethod
    def _list_field(value: object) -> list[str]:
        """把 frontmatter 中的列表字段归一化为字符串列表。

        由 LLM 编写的 Agent 文件常把列表写成标量，例如 `tools: "shell,
        filesystem"`。标量不能原样返回——``Agent.__init__`` 会逐项遍历
        ``config.tools``，若传入字符串就会按字符遍历，从而静默丢掉所有工具。
        这里与 ``memory_rw._coerce_tags`` 采用相同防御：去除空白后按逗号切分。
        """
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return []

    @staticmethod
    def _as_bool(value: object) -> bool:
        """归一化 frontmatter 中的布尔值。

        YAML 作者常会给标量加引号，而 ``bool("false")`` 结果为 True——带引号
        的 ``api_request_no_confirm: "false"`` 会静默关闭 api_request 的写操作
        确认闸门。语义与 ``scheduler._as_bool`` 保持一致。
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)

    @staticmethod
    def _as_int(value: object, default: int) -> int:
        """强制转换 frontmatter 中的整数；空白或格式错误时回退到默认值。

        ``max_tokens: "64000"`` 是字符串，而 ``max_tokens:`` 为空时是 None；
        二者若进入 ``min(max_tokens, MAX_OUTPUT_RESERVE)`` 都会抛出
        TypeError，使每一轮都以 api_error 结束。
        """
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @classmethod
    def from_file(cls, path: str) -> "AgentConfig":
        post = frontmatter.load(path)
        meta = post.metadata
        # 中文优先的规范：当前所有 Agent 定义的描述都是中文；多语言映射
        # 必须折叠为同一文本，使委派名单/历史前缀看不到任何变化。
        description, _ = normalize_i18n_field(
            meta.get("description", ""), canonical_order=("zh-CN", "en-US")
        )
        return cls(
            slug=meta["slug"],
            description=description or "",
            system_prompt=post.content,
            tools=cls._list_field(meta.get("tools")),
            skills=cls._list_field(meta.get("skills")),
            workflows=cls._list_field(meta.get("workflows")),
            knowledge=cls._list_field(meta.get("knowledge")),
            max_tokens=cls._as_int(meta.get("max_tokens"), 128000),
            token_budget=cls._as_int(meta.get("token_budget"), 100000),
            api_request_no_confirm=cls._as_bool(meta.get("api_request_no_confirm", False)),
        )


class Agent:
    """主编排器（Agent）。

    支持两种运行模式：
    - 对话模式（Chat）：单次 LLM 调用 + 工具调用循环；
    - 任务模式（Task）：先由 plan_task 规划任务，再依次执行每个任务。
    """

    # 等待 API 处理器对"注入的用户消息"返回持久化确认的时间上限。超时会
    # 降级为成功处理（在数据持久性与流程推进之间取平衡）。
    _INJECT_PERSIST_TIMEOUT = 30.0

    def __init__(
        self,
        config: AgentConfig,
        credentials: dict[str, dict],  # {config_id: {api_key, base_url?, ...}}
        tier_models: dict[str, dict],  # {config_id: {provider, model}}
        skill_registry: SkillRegistry,
        tool_context: ToolContext,
        project_context: KnowledgeContextResult | None = None,
        db_session: Any = None,
        pending_task_context: str = "",
        personal_memory_context: str = "",
        active_plan_context: str = "",
        workflow_registry: Any = None,
        tier_map: dict[str, str] | None = None,  # {complexity tier: config_id}, auto-route mode
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._tier_models = tier_models
        self._tier_map = tier_map
        self._skill_registry = skill_registry
        self._workflow_registry = workflow_registry
        self._tool_ctx = tool_context
        self._project_context = project_context
        self._db = db_session
        self._pending_task_context = pending_task_context
        self._personal_memory_context = personal_memory_context
        self._active_plan_context = active_plan_context
        # 过滤掉 MCP 标记（mcp / mcp:<slug>）——它们会在 run() 之前由
        # attach_mcp_tools() 解析为动态工具实例。
        static_tool_names = [t for t in config.tools if not t.startswith("mcp")]
        self._tools = build_tool_registry(static_tool_names)
        self._turn = 0
        self._current_task_id: str | None = None
        self._prompt_dirty = False
        # _prompt_dirty 是一个共享布尔值——在并行任务下，任务 A 的改动会
        # 标记它，任务 B 的下一次迭代会消费并清除它，导致 A 的提示词无法
        # 重建。_prompt_revision 让每个循环都能独立重建（见 _mark_prompt_dirty）。
        self._prompt_revision = 0
        self._static_prompt_cache: str | None = None
        # 本轮用户消息命中触发条件的技能。保存在实例上（而非追加到局部
        # 提示词字符串），使 plan_task 流水线内的提示词重建仍能保留技能
        # 正文——一旦 _mark_prompt_dirty 强制重建，局部追加的内容就会丢失。
        self._active_skills: list = []
        self._provider_cache: dict[str, LLMProvider] = {}
        self._task_plan: list[dict] | None = None  # 实时任务状态跟踪
        for tool in self._tools.values():
            self._tool_ctx.register_tool(tool)
        # 把项目上下文关联到工具上下文
        if project_context is not None:
            self._tool_ctx.project_context = project_context

    def add_tools(self, tools: dict[str, Tool]) -> None:
        """在构造完成后注入动态工具实例。

        由 WebSocket 处理器调用，在 ``run()`` 快照 ``tool_defs`` 之前挂上
        MCP 发现的工具。已存在的工具名优先（内置工具绝不会被 MCP 工具覆盖）。
        """
        import logging as _logging
        _log_a = _logging.getLogger("agent_core.agent")
        for name, tool in tools.items():
            if name in self._tools:
                _log_a.warning("Tool name collision, skipping dynamic tool: %s", name)
                continue
            self._tools[name] = tool
            self._tool_ctx.register_tool(tool)

    async def close(self) -> None:
        """释放模型供应方的 HTTP 客户端与缓存连接。

        可安全重复调用。run() 结束后必须 await 本方法，避免每条消息新建的
        Agent 实例泄漏 httpx.AsyncClient 连接。
        """
        for provider in self._provider_cache.values():
            try:
                await provider.close()
            except Exception:
                pass
        self._provider_cache.clear()

    def _get_provider(self, config_id: str) -> LLMProvider:
        """为指定 config_id 实例化（或取回缓存的）LLM 模型供应方。"""
        model_cfg = self._tier_models.get(config_id)
        if not model_cfg:
            if not self._tier_models:
                raise RuntimeError(
                    "No model configured. Go to Settings → AI Models to add a provider."
                )
            config_id = next(iter(self._tier_models))
            model_cfg = self._tier_models[config_id]
        provider_type = model_cfg.get("provider", "openai")
        model_name = model_cfg.get("model", "")
        cache_key = f"{config_id}:{model_name}"
        if cache_key in self._provider_cache:
            return self._provider_cache[cache_key]
        creds = self._credentials.get(config_id, {})
        merged = {**creds, **{k: v for k, v in model_cfg.items() if k != "provider"}}
        provider = get_provider(provider_type, merged)
        self._provider_cache[cache_key] = provider
        return provider

    @staticmethod
    def _merge_tool_args(existing: str, delta: str) -> str:
        """合并流式工具调用的参数片段。

        OpenAI 风格的供应方把一个 JSON 文档拆到多个数据块（直接字符串拼接
        即可）；Gemini 则按数据块发送增量字段 dict，朴素拼接会产生
        ``{"a":1}{"b":2}`` 这样的非法 JSON。只要拼接文本本身已是一个合法
        JSON 文档，就优先采用拼接；仅在拼接无法解析时才尝试 dict 合并
        （因此恰好落在 JSON 边界上的分片切分不会改变参数语义）。结果长度
        以 _MAX_TOOL_ARGS_CHARS 为上限。
        """
        if not delta:
            return existing
        if not existing:
            return delta
        combined = existing + delta
        try:
            json.loads(combined)
            result = combined
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                base = json.loads(existing)
                add = json.loads(delta)
                if isinstance(base, dict) and isinstance(add, dict):
                    merged = dict(base)
                    merged.update(add)
                    result = json.dumps(merged, ensure_ascii=False)
                else:
                    result = combined
            except (json.JSONDecodeError, TypeError, ValueError):
                result = combined
        if len(result) > _MAX_TOOL_ARGS_CHARS:
            _log.warning("Tool call arguments exceeded %d chars; truncating", _MAX_TOOL_ARGS_CHARS)
            result = result[:_MAX_TOOL_ARGS_CHARS]
        return result

    @staticmethod
    def _parse_tool_arguments(tc: dict):
        """解析一次工具调用的流式参数，空白表示无参数。

        Anthropic 与 Gemini 会把零参数调用以空参数字符串流式返回——没有
        任何 JSON 分片需要界定——因此空白值必须解释为 ``{}``，而非非法
        JSON。同时归一化存储的调用，使下一次供应方请求也携带合法 JSON。
        对真正错误的值抛出 ``JSONDecodeError``/``TypeError``：解析出的载荷
        必须是 JSON 对象。否则格式良好的数组/null/字符串会进入
        knowledge_rw/memory_rw 执行后钩子中的 ``args.get(...)``，而这些钩子
        位于工具 try/except 之外——非 dict 值会以 AttributeError 让整轮
        崩溃，而不是作为模型可自行纠正的工具错误暴露出来。
        """
        raw = tc["function"].get("arguments")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            tc["function"]["arguments"] = "{}"
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(
                f"tool arguments must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed

    @staticmethod
    def _accumulate_tool_delta(buffer: dict, delta: dict) -> None:
        """把一个流式工具调用增量数据块累积进按索引（index）区分的缓冲区。"""
        idx = delta.get("index", 0)
        if idx not in buffer:
            buffer[idx] = {"id": delta.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
        tc = buffer[idx]
        new_id = delta.get("id", "")
        if new_id:
            tc["id"] = new_id
        fn = delta.get("function", {})
        if fn.get("name"):
            tc["function"]["name"] = fn["name"]
        tc["function"]["arguments"] = Agent._merge_tool_args(
            tc["function"].get("arguments", ""), fn.get("arguments", "")
        )

    @staticmethod
    def _drop_orphan_tool_messages(messages: list[Message]) -> list[Message]:
        """移除缺少对应助手 tool_calls 配对的工具消息。

        运行中断或历史切片可能导致工具结果丢失了前置的助手 tool_calls
        消息；OpenAI 兼容供应方会以 400 拒绝这种组合。尾部悬空的助手
        tool_calls（其工具结果始终未到达）同样会被拒绝，因此一并丢弃。
        历史中间的配对予以保留——循环总是在发起调用的助手消息之后立即
        追加工具结果。
        """
        pending: set[str] = set()
        cleaned: list[Message] = []
        for message in messages:
            if message.role == "assistant":
                for call in message.tool_calls or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        pending.add(call_id)
            elif message.role == "tool":
                if message.tool_call_id not in pending:
                    _log.warning(
                        "Dropping orphan tool message (tool_call_id=%s)", message.tool_call_id
                    )
                    continue
                pending.discard(message.tool_call_id)
            cleaned.append(message)
        while cleaned and cleaned[-1].role == "assistant" and cleaned[-1].tool_calls:
            _log.warning(
                "Dropping dangling assistant tool_calls message (tool_calls=%s)",
                [str(c.get("id")) for c in cleaned[-1].tool_calls],
            )
            cleaned.pop()
        return cleaned

    @staticmethod
    def _history_tool_call_summary(messages: list[Message]) -> tuple[str, list[str]]:
        """返回工具调用历史的紧凑、密钥安全摘要。"""
        pending: set[str] = set()
        roles: list[str] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                tool_names = [
                    str(call.get("function", {}).get("name", "?"))
                    for call in message.tool_calls
                ]
                roles.append(f"assistant[{','.join(tool_names)}]")
                pending.update(
                    str(call.get("id"))
                    for call in message.tool_calls
                    if call.get("id")
                )
            elif message.role == "tool":
                roles.append(f"tool[{message.name or '?'}]")
                if message.tool_call_id:
                    pending.discard(message.tool_call_id)
            else:
                roles.append(message.role)
        return " -> ".join(roles), sorted(pending)

    @staticmethod
    def _build_task_messages(
        messages: list[Message],
        plan_tool_id: str,
        task_title: str,
        dependency_results: list[tuple[str, str]] | None = None,
    ) -> list[Message]:
        """为执行单个规划任务，分叉出一份合法的工具结果历史。

        外层循环在所有任务完成之前无法追加其最终计划摘要，但嵌套的任务
        请求在加入其任务专属用户消息之前，仍必须先确认外层的 plan_task 调用。

        `dependency_results` 携带每个已完成依赖的（标题, 摘要）——缺少它，
        依赖任务会重新推导前置任务已经证明的事实（例如再次探测一个已返回
        201 的 API）。
        """
        task_content = f"Execute this task: {task_title}"
        if dependency_results:
            deps_block = "\n\n".join(
                f"### Completed dependency: {title}\n{summary}"
                for title, summary in dependency_results
            )
            task_content = (
                "Results from previously completed dependency tasks:\n\n"
                f"{deps_block}\n\n{task_content}"
            )
        return messages + [
            Message(
                role="tool",
                content=json.dumps({
                    "status": "accepted",
                    "message": "Plan accepted; task execution is in progress.",
                }),
                tool_call_id=plan_tool_id,
                name="plan_task",
            ),
            Message(role="user", content=task_content),
        ]

    @staticmethod
    def _normalize_task_plan(tasks: list[dict]) -> list[dict]:
        """把规划器本地的任务 ID 改写为全局唯一 ID，以便持久化。"""
        # 模式（schema）声明的是对象数组，但模型也可能输出裸字符串或字符串
        # 列表；对这些值调用 .get()/dict() 会抛出 AttributeError/ValueError，
        # 表现为含义不明的 "plan_task failed"，而非模型可据此行动的提示。
        if not isinstance(tasks, list):
            raise ValueError("tasks must be a list of objects")
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError(
                    f"each task must be an object, got {type(task).__name__}"
                )
        original_ids = [str(task.get("id") or f"task-{idx}") for idx, task in enumerate(tasks)]
        task_ids = [str(uuid.uuid4()) for _ in tasks]
        id_map: dict[str, str] = {}
        for original_id, task_id in zip(original_ids, task_ids, strict=True):
            id_map.setdefault(original_id, task_id)

        normalized: list[dict] = []
        for task, original_id, task_id in zip(tasks, original_ids, task_ids, strict=True):
            normalized_task = dict(task)
            normalized_task["id"] = task_id
            depends_on = normalized_task.get("depends_on")
            if isinstance(depends_on, list):
                normalized_task["depends_on"] = [
                    id_map.get(str(dep), str(dep)) for dep in depends_on
                ]
            elif isinstance(depends_on, str):
                # LLM 把依赖列表字符串化了——需要切分，否则下方构建 DAG 时
                # 会按字符逐个遍历，静默丢掉所有依赖（导致任务过早执行）。
                normalized_task["depends_on"] = [
                    id_map.get(str(dep), str(dep))
                    for dep in depends_on.split(",") if dep.strip()
                ]
            else:
                # null / 缺失 / 格式错误：模式要求是数组，但模型会输出 null，
                # 该键随后以 None 值保留下来——而每个消费方都会遍历它。
                normalized_task["depends_on"] = []
            normalized.append(normalized_task)

        return normalized

    @staticmethod
    def _build_dependency_graph(
        tasks: list[dict],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """基于归一化任务列表构建正向与反向依赖映射。

        返回：
            deps：       {任务 ID -> 仍待满足的依赖 ID 集合}
            dependents： {依赖 ID -> 依赖它的任务 ID 集合}

        未知的依赖 ID（不在任务列表中）会被静默丢弃。
        若检测到依赖环，则抛出 ``ValueError``。
        """
        task_ids = {str(t["id"]) for t in tasks}
        deps: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {tid: set() for tid in task_ids}

        for t in tasks:
            tid = str(t["id"])
            raw_deps = t.get("depends_on") or []
            if isinstance(raw_deps, str):
                # 与 _normalize_task_plan 相同的防御：字符串化的列表会在
                # 下方被按字符逐个遍历。
                raw_deps = [d.strip() for d in raw_deps.split(",") if d.strip()]
            valid = {str(d) for d in raw_deps if str(d) in task_ids}
            deps[tid] = set(valid)
            for dep_id in valid:
                dependents[dep_id].add(tid)

        # 用 Kahn 算法（拓扑排序）检测依赖环
        in_degree = {tid: len(d) for tid, d in deps.items()}
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            current = queue.pop()
            visited += 1
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        if visited != len(task_ids):
            raise ValueError("Cycle detected in task dependencies")

        return deps, dependents

    _TASK_SOURCE_PRIORITY_SKILL = "integration/task-source-priority"

    def _task_source_priority(self, user_message: str) -> str | None:
        """当消息命中已知实体锚点（Jira 编号 / 拉取请求）时，返回简短路由指令。

        表驱动：条目来自 task-source-priority 技能的 `routing` frontmatter，
        因此新的集成（如 GitLab 议题）只需编辑该文件即可扩展行为，无需改
        代码。该指令还以 Agent 确实声明了被映射工具为前提；未声明该工具的
        Agent（office-assistant、data-analyst）不会得到任何指令。
        """
        prio_skill = self._skill_registry.get(self._TASK_SOURCE_PRIORITY_SKILL)
        if not prio_skill or not prio_skill.routing:
            return None

        # frontmatter 是 Agent 可编辑的界面：非数字优先级或格式错误的正则
        # 锚点都必须降级为跳过该条目，绝不抛出异常——这段逻辑在 run() 中
        # 工具循环之前无条件执行，此处异常会导致后续每一轮用户消息都失败。
        def _priority(entry: object) -> int:
            if not isinstance(entry, dict):
                return 0
            try:
                return int(entry.get("priority", 0))
            except (TypeError, ValueError):
                return 0

        entries = sorted(
            (e for e in prio_skill.routing if isinstance(e, dict)),
            key=_priority, reverse=True,
        )
        for entry in entries:
            anchors = entry.get("anchors") or []
            if isinstance(anchors, str):
                anchors = [anchors]
            elif not isinstance(anchors, (list, tuple)):
                anchors = []
            matched = False
            for a in anchors:
                if not isinstance(a, str):
                    continue
                try:
                    if re.search(a, user_message, re.IGNORECASE):
                        matched = True
                        break
                except re.error:
                    _log.warning(
                        "task-source-priority: invalid anchor regex %r skipped",
                        a,
                    )
            if not matched:
                continue
            tool = entry.get("tool")
            if not isinstance(tool, str) or tool not in self._tools:
                continue
            raw_ops = entry.get("first_ops") or []
            if isinstance(raw_ops, str):
                first_ops = raw_ops
            elif isinstance(raw_ops, (list, tuple)):
                first_ops = " → ".join(str(op) for op in raw_ops)
            else:
                first_ops = ""
            parts = [
                f"This task references {entry.get('label') or tool}. "
                f"Call the `{tool}` tool FIRST: {first_ops}."
            ]
            raw_follow_ups = entry.get("follow_ups") or []
            if isinstance(raw_follow_ups, dict):
                raw_follow_ups = [raw_follow_ups]
            elif not isinstance(raw_follow_ups, (list, tuple)):
                raw_follow_ups = []
            follow_ups = []
            for fu in raw_follow_ups:
                if not isinstance(fu, dict):
                    continue
                fu_tool = fu.get("tool")
                if not isinstance(fu_tool, str):
                    continue
                if fu_tool and fu_tool not in self._tools:
                    continue
                text = f"use `{fu_tool}`: {fu.get('op')}"
                if fu.get("note"):
                    text += f" — {fu['note']}"
                follow_ups.append(text)
            if follow_ups:
                parts.append("Then " + "; ".join(follow_ups) + ".")
            parts.append(
                "Do not precede the first authoritative call with `git_repo` "
                "list_repos/status/pull exploration."
            )
            return "\n".join(parts)
        return None

    async def run(
        self,
        user_message: str,
        history: list[Message],
        session: ConversationSession,
        provider_override: str | None = None,
        attachments: list[dict] | None = None,
        auto_tier: str | None = None,
    ) -> None:
        """单个用户轮次的入口；通过 session 向外发出事件。

        attachments：已准备好的附件记录（{name, media_type, size, rel_path,
        inline_text?, image_data?, image_media_type?}）——由 API 处理器校验，
        并从磁盘读取文件内容。

        auto_tier：自动路由模式下，调用方预分类器为本轮分配的复杂度层级
        （"low"/"mid"/"high"）；在 model_selected 事件上暴露给前端。显式
        选择模型时为 None。
        """
        # 递增轮次计数器，并同步到工具上下文
        self._turn += 1
        self._tool_ctx.current_turn = self._turn
        self._tool_ctx.current_task_id = self._current_task_id

        # 1. 通过 override 或第一个可用配置来选定模型
        config_id = provider_override if (provider_override and provider_override in self._tier_models) else None
        if not config_id:
            if not self._tier_models:
                raise RuntimeError("No model configured. Go to Settings → AI Models to add a provider.")
            config_id = next(iter(self._tier_models))
        model_cfg = self._tier_models.get(config_id, {})
        provider_key = model_cfg.get("provider", "openai")
        await session.emit("model_selected", provider=provider_key, model=model_cfg.get("model", ""), tier=auto_tier)

        # 2. 发出上下文（知识库）占用信息
        if self._project_context and self._project_context.loaded_entries:
            await session.emit(
                "knowledge_loaded",
                files=[e.slug for e in self._project_context.loaded_entries],
            )

        # 3. 匹配本轮技能（仅对当前轮有效，每次 run 都重置），然后构建系统
        # 提示词——_build_dynamic_prompt 会注入技能正文，因此任务循环中途
        # 重建提示词时仍能保留这些内容。
        self._active_skills = self._skill_registry.matching_triggers(user_message)[:3]
        system = self._build_system_prompt()

        # 4b. 任务来源优先级指令：当消息引用 Jira 编号/PR 锚点时，告诉模型
        # 哪个工具是权威的首个调用（由 task-source-priority 技能表驱动）。
        priority = self._task_source_priority(user_message)
        if priority:
            system += f"\n\n## Task Source Priority\n{priority}"

        # 5. 仅依据模型供应方的上下文窗口压缩历史。会话预算只是用量展示值，
        # 并非运行时限制。
        _log.info("agent.run: step5 compress_history start, provider=%s", provider_key)
        try:
            import asyncio as _asyncio
            comp_provider = self._get_provider(config_id)
            tool_defs = [t.to_definition() for t in self._tools.values()]
            reserved_input = (
                estimate_history_tokens([Message(role="system", content=system), Message(role="user", content=user_message)])
                + sum(estimate_history_tokens([Message(role="system", content=f"{t.name}\n{t.description}\n{t.parameters}")]) for t in tool_defs)
            )
            available_budget = compression_budget(
                comp_provider.context_window, reserved_input, self._config.max_tokens
            )
            if available_budget <= 0:
                # 仅输入部分（系统提示 + 用户消息 + 工具定义）就已超出窗口
                # ——压缩没有可用空间（其摘要 + 近期尾部同样会超限），而
                # `max(budget, 1)` 会在阈值为 0 时每轮都强制压缩，无谓失败。
                # 跳过压缩，交由供应方的 context_exceeded 处理接管。
                _log.warning(
                    "agent.run: skip compression — reserved context (%d) exceeds window (%d)",
                    reserved_input + min(self._config.max_tokens, MAX_OUTPUT_RESERVE), comp_provider.context_window,
                )
            else:
                # 字节感知：Token 阈值可能漏掉字节密集型历史（中日韩字符的
                # JSON 转义、base64 图片）。若组装后的请求会超过网关字节
                # 上限，即使 Token 未达阈值，也强制摘要早期历史。
                _probe_messages = (
                    [Message(role="system", content=system)]
                    + history
                    + [Message(role="user", content=user_message)]
                )
                byte_over = (
                    estimate_request_bytes(_probe_messages, tool_defs) > MAX_REQUEST_BYTES
                )
                async with _asyncio.timeout(60):
                    # 阶段通知：压缩可能静默占用长达 60 秒——没有它，前端
                    # 会看到一个无响应的连接。
                    await session.emit("turn_status", phase="compressing")
                    history = await compress_history(
                        history, available_budget, comp_provider, force=byte_over
                    )
        except TimeoutError:
            _log.warning("History compression timed out, keeping recent messages only")
            await session.emit("warning", message="History compression timed out, keeping recent messages only")
            from .compressor import RECENT_TURNS_KEEP
            if len(history) > RECENT_TURNS_KEEP:
                system_msgs = [m for m in history if m.role == "system"]
                trimmed = history[-RECENT_TURNS_KEEP:]
                # 与 compressor.split_early_recent 相同的成对弹出：工具消息
                # 不能位于请求开头，助手 tool_calls 也不能在没有工具结果的
                # 情况下悬空（否则供应方返回 400）。
                while trimmed and (
                    trimmed[0].role == "tool"
                    or (trimmed[0].role == "assistant" and trimmed[0].tool_calls)
                ):
                    trimmed.pop(0)
                history = system_msgs + trimmed
        except Exception as e:
            _log.warning(
                "History compression failed, using full history: %s",
                _scrub_credentials(traceback.format_exc(), self._credentials),
            )
            await session.emit(
                "warning",
                message=f"History compression failed, using full history: "
                        f"{_scrub_credentials(str(e), self._credentials)}",
            )

        # 6. 构建消息（附件会使用户内容变为多模态分片列表）
        provider = self._get_provider(config_id)
        messages = [Message(role="system", content=system)] + history + [
            Message(role="user", content=build_user_content(user_message, attachments, provider.supports_vision))
        ]

        # 7. 构建工具定义
        tool_defs = [t.to_definition() for t in self._tools.values()]

        # 8. 运行对应的循环
        _log.info("agent.run: step8 _run_loop start, provider=%s", provider_key)
        await self._run_loop(messages, tool_defs, provider, session, config_id)
        _log.info("agent.run: step8 _run_loop done")

    def _mark_prompt_dirty(self) -> None:
        """记录系统提示词在下一轮之前必须重建。

        仅靠 _prompt_dirty 时它是共享布尔值——并行任务下，任务 A 的知识库
        改动设置了它，任务 B 的下一次迭代看到标志、重建 B 的提示词并清除
        标志，导致 A 的提示词始终得不到刷新（随机、取决于调度的陈旧问题）。
        单调递增的版本号让每个循环都能独立重建。
        """
        self._prompt_dirty = True
        self._prompt_revision += 1

    def _build_system_prompt(self) -> str:
        """构建完整系统提示词：缓存的静态部分 + 动态部分。"""
        static = self._get_static_prompt()
        dynamic = self._build_dynamic_prompt()
        return static + dynamic if dynamic else static

    def _get_static_prompt(self) -> str:
        """返回缓存的静态部分（Agent 提示词 + 静态知识 + 记忆/计划/任务上下文）。"""
        if self._static_prompt_cache is not None:
            return self._static_prompt_cache

        parts = [self._config.system_prompt]

        workspace = self._tool_ctx.project_fs_path
        # 工作区目录名即项目 slug——项目级定义的命名规则依赖于此，而当需求
        # 在别处说明时，"路径最后一段"很容易被忽略。这里复用写入时定义
        # 检查所用的同一个辅助函数，使此处声明的规则与彼处强制的规则不会
        # 不一致。在此处导入是为了避开本模块"导入前即写代码"的代码块。
        from .definition_check import project_slug_from_workspace

        project_slug = project_slug_from_workspace(workspace) or ""
        slug_note = (
            f"Project-scoped definitions belong in `agents/`, `skills/`, and `workflows/` under "
            f"the workspace; project agent files are named `{project_slug}--<name>.agent.md` with a "
            f"frontmatter `slug` equal to the filename stem (this workspace's project slug is "
            f"`{project_slug}`). "
            if project_slug else ""
        )
        parts.append(
            f"\n## Workspace Convention\n"
            f"Your workspace root is `{workspace}`. "
            "All filesystem and shell paths are relative to this root. "
            "Key directories: `knowledge/`, `tests/generated/`, `.tmp/media/`, `.tmp/work/`. "
            f"{slug_note}"
            "Files you write to the code_executor `OUTPUT_DIR`, or hand over with the "
            "`deliver_file` tool, appear in the chat as images or downloadable "
            "attachments for the user. "
            "Never use `../`, absolute paths, or `projects/<slug>/` prefixes to access "
            "files outside this workspace - sibling project directories are invisible to you. "
            "Cross-project access is blocked by the filesystem, shell, and code_executor tools; "
            "a rejection from these tools is expected behavior, not an error to work around. "
            "If you need information from another project, ask the user."
        )

        parts.append(
            "\nKnowledge write priority (知识写入优先级): when learning or back-writing knowledge, "
            "prefer updating an existing project knowledge file — the files listed under "
            "'## Project Knowledge' below, including template files still holding '(to be filled …)' "
            "placeholders; an empty template file means the project expects that file to be maintained. "
            "创建新文件仅在内容不归入任何已有文件、且通过 knowledge-manager 技能的资格判断时才允许。"
            "合法例外：每个服务一个的明细文件 technical/api/{service-slug}.md。"
            "Never create a parallel file for content an existing file already covers."
        )

        parts.append(
            "\n## Interaction Style\n"
            "Before calling any tool, first briefly state what you are about to do and why "
            "(one sentence is enough). After a tool returns, summarize the result and state "
            "what you will do next before calling the next tool. "
            "Never call tools silently — the user should always know the current step."
        )

        parts.append(self._build_tools_prompt())

        parts.append(
            "\n## Incremental Execution Protocol\n"
            "For implementation or multi-step work, call `plan_task` before making changes. "
            "Break the goal into small, independently reviewable tasks; each task must name "
            "its expected output and a concrete verification method. Complete one task at a "
            "time: inspect the relevant context, make only that task's change, run its stated "
            "verification, and report the result before starting the next task. Do not mark a "
            "task complete when its verification failed or was skipped; record the blocker or "
            "remaining work instead. Keep task summaries factual so they can be used after "
            "history compression.\n\n"
            "When creating a Git commit, first inspect the working tree and select only the "
            "files belonging to the verified task. A commit is an externally meaningful, "
            "hard-to-reverse action: it always requires an explicit user confirmation immediately "
            "before the commit. Never commit unrelated changes or commit merely because a task "
            "is finished."
        )

        if self._project_context:
            ctx = self._project_context
            if ctx.loaded_content:
                parts.append("\n## Project Knowledge")
                for slug, content in ctx.loaded_content.items():
                    parts.append(f"\n### [[{slug}]]\n{content}")

            if ctx.overflow_slugs:
                parts.append(
                    "\n## Additional Knowledge (use knowledge_rw read)\n"
                    + "\n".join(f"- [[{s}]]" for s in ctx.overflow_slugs)
                )

        if self._personal_memory_context:
            parts.append(f"\n{self._personal_memory_context}")

        if self._active_plan_context:
            parts.append(f"\n{self._active_plan_context}")

        if self._pending_task_context:
            parts.append(f"\n{self._pending_task_context}")

        if self._config.workflows and self._workflow_registry:
            wf_parts = []
            for wf_slug in self._config.workflows:
                defn = self._workflow_registry.get(wf_slug)
                if defn:
                    wf_parts.append(f"- **{wf_slug}**: {defn.description}")
            if wf_parts:
                parts.append("\n## Available Workflows\n" + "\n".join(wf_parts))
                parts.append(
                    '\nTo follow a workflow, use `filesystem read_file` on '
                    '`workflows/<slug>.workflow.md` for detailed steps.'
                )

        self._static_prompt_cache = "\n".join(parts)
        return self._static_prompt_cache

    def _build_tools_prompt(self) -> str:
        """基于 self._tools 构建"## Available Tools & Behaviors（可用工具与行为）"章节。"""
        lines = [
            "\n## Available Tools & Behaviors",
            "You have exactly the tools listed below. Do not attempt operations none of "
            "them cover (e.g. raw network access, package installation, host system "
            "changes) — they will fail.",
        ]
        for tool in self._tools.values():
            hint = tool.prompt_hint or tool.description.split(". ")[0].split("\n")[0]
            lines.append(f"- **{tool.name}** — {hint}")
        lines.append(
            "\nBehavior rules:\n"
            "- Prefer the purpose-built tool over a shell equivalent (e.g. filesystem "
            "read over `cat`).\n"
            "- A tool rejection or sandbox block is expected behavior: follow the reason "
            "given and switch tools. Never retry the same operation with rephrased "
            "commands or flags.\n"
            "- If no listed tool can do what you need, tell the user instead of improvising."
        )
        return "\n".join(lines)

    def _build_dynamic_prompt(self) -> str:
        """构建动态部分（活跃技能 + 任务计划 + 动态加载知识 + 待加载明细表）。"""
        parts: list[str] = []

        # 本轮命中触发条件的技能——放在动态章节，使提示词重建（plan_task
        # 循环、知识写入）时绝不会丢失它们。
        if self._active_skills:
            for skill in self._active_skills:
                parts.append(f"\n## Active Skill: {skill.slug}\n{skill.body}")

        # 实时任务计划（随任务完成而更新）
        if self._task_plan:
            status_icon = {"completed": "✓", "running": "→", "failed": "✗", "skipped": "-"}
            parts.append("\n## Current Task Plan")
            for t in self._task_plan:
                icon = status_icon.get(t.get("status", "pending"), " ")
                title = t.get("title", "")
                status = t.get("status", "pending")
                suffix = f" ({status})" if status not in ("pending", "completed") else ""
                parts.append(f"[{icon}] {title}{suffix}")

        if self._project_context:
            ctx = self._project_context

            if ctx.dynamically_loaded:
                parts.append("\n## Dynamically Loaded Knowledge")
                parts.append("These persist across turns until released with `knowledge_rw unload`.\n")
                for slug, content in ctx.dynamically_loaded.items():
                    record = ctx.dynamic_records.get(slug)
                    task_info = f"(task: {record.task_id})" if record and record.task_id else "(manual)"
                    parts.append(f"\n### [[{slug}]] {task_info}\n{content}")

            if ctx.deferred_entries:
                parts.append("\n## Available Detail Knowledge")
                parts.append('Use `knowledge_rw load slug="..."` to load into context.\n')
                parts.append("| Slug | Summary | Parent |")
                parts.append("|------|---------|--------|")
                for slug, entry in ctx.deferred_entries.items():
                    parts.append(f"| `{slug}` | {entry.summary} | {entry.parent_slug or '-'} |")

        return "\n".join(parts) if parts else ""

    def _invalidate_static_cache(self) -> None:
        """当静态知识发生变化（写入静态文件）时调用，清空静态提示词缓存。"""
        self._static_prompt_cache = None

    async def _consume_injected_input(
        self,
        session: ConversationSession,
        messages: list[Message],
        provider: LLMProvider,
        message_id: str,
    ) -> str | None:
        """在步骤边界消费所有排队中的用户消息。

        当前步骤的部分输出会被定稿为"interrupted（已中断）"快照，用户消息
        由 API 处理器持久化（user_message_injected 事件），随后循环把新指令
        追加到消息历史并继续。返回新的消息 ID；若无排队消息则返回 None。
        """
        entries: list[UserInputEntry] = []
        while True:
            entry = session.dequeue_user_input()
            if entry is None:
                break
            entries.append(entry)
        if not entries:
            return None

        # 把当前部分输出快照为一条"已中断"消息（由 forward_events 持久化）。
        # emitted_end 保持 False——本轮仍在继续，不能被 finally 块重复定稿。
        # injection=True 标记这是一个步骤边界，而非终止性中断：API 不能在此
        # 结算本次会话运行，否则其状态会永远停在 "interrupted"（真正的终止
        # stream_end 在与 finish_run 仅处理 running 的守卫竞争中落败），前端
        # 也会一直显示"上次运行被中断"。
        await session.emit(
            "stream_end",
            message_id=message_id,
            total_tokens=session.tokens_used,
            stop_reason="interrupted",
            injection=True,
        )

        # 现在请处理器持久化用户消息——事件顺序保证它们落在上方中断快照之后。
        for entry in entries:
            await session.emit(
                "user_message_injected",
                message_id=entry.message_id,
                content=entry.content,
                attachments=entry.attachments,
                agent_id=entry.agent_id,
                config_id=entry.config_id,
            )

        async def _await_persist(entry: UserInputEntry) -> bool:
            if entry.persisted is None:
                return True
            try:
                return await asyncio.wait_for(
                    asyncio.shield(entry.persisted), timeout=self._INJECT_PERSIST_TIMEOUT
                )
            except asyncio.TimeoutError:
                # 超时只约束数据持久性，不约束 Agent 流程——降级为成功，避免
                # 卡住的处理器让循环停滞。这里绝不能捕获 CancelledError：它
                # 意味着本轮本身正被拆除（中止监视器/关闭），吞掉它会让循环
                # 在会话已关闭后仍追加条目并发起下一次 LLM 调用。
                return True

        oks = await asyncio.gather(*[_await_persist(e) for e in entries])
        accepted = [e for e, ok in zip(entries, oks) if ok]
        for entry in accepted:
            messages.append(Message(
                role="user",
                content=build_user_content(
                    entry.content, entry.attachment_records or [], provider.supports_vision
                ),
            ))
            entry.consumed = True
        if not accepted:
            # 所有排队消息都未通过校验——但上方的中断 stream_end 已针对当前
            # ID 发出，处理器也持久化了该快照行。若沿用同一 ID 继续，下一次
            # stream_end 会插入主键相同的第二行：插入被吞掉、本轮输出丢失、
            # 运行行仍停在 "running"。因此换一个新 ID 继续。
            _log.warning(
                "agent: %d injected message(s) rejected — continuing under a new message id",
                len(entries),
            )
        return session.new_message()

    def _request_byte_outcome(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        tool_wire_bytes: int | None = None,
    ) -> str:
        """强制网络传输字节上限：就地截断超大的工具消息（内存中的处理；
        手动压缩路径会将其持久化），返回 "ok" / "truncated" / "over_limit"。

        tool_wire_bytes 携带 tool_defs 预先算好的传输尺寸，使逐次迭代调用
        不必重新估算静态的工具模式。"""
        outcome = force_compress_history(
            messages, MAX_REQUEST_BYTES, tool_defs, tool_wire_bytes=tool_wire_bytes
        )
        if outcome == "truncated":
            _log.warning("agent: request byte guard truncated oversized tool outputs")
        return outcome

    def _over_limit_message(self, messages: list[Message], tool_defs: list[ToolDefinition]) -> str:
        """截断后载荷仍超限时，返回面向用户的提示文本（此时主要由系统提示
        知识、近期文本或附件占据体积）。"""
        size_mb = estimate_request_bytes(messages, tool_defs) / (1024 * 1024)
        limit_mb = MAX_REQUEST_BYTES // (1024 * 1024)
        bd = request_byte_breakdown(messages, tool_defs)
        msg = (
            f"请求内容过大(约 {size_mb:.1f} MB,上限 {limit_mb} MB),"
            "自动精简后仍超出限制,已停止发送。"
        )
        # 可操作的归因：指出占比最大的部分，让用户知道该精简知识文件、
        # 去掉附件还是重新开始。
        parts = []
        if bd["system"]:
            parts.append(f"知识/系统提示约 {bd['system'] / (1024 * 1024):.1f} MB")
        if bd["images"]:
            parts.append(f"附件图片约 {bd['images'] / (1024 * 1024):.1f} MB")
        if bd["text"]:
            parts.append(f"历史文本约 {bd['text'] / (1024 * 1024):.1f} MB")
        if bd["tools"]:
            parts.append(f"工具定义约 {bd['tools'] / (1024 * 1024):.1f} MB")
        if parts:
            msg += " 构成:" + "、".join(parts) + "。"
        if self._project_context and self._project_context.loaded_entries:
            msg += f" 当前加载 {len(self._project_context.loaded_entries)} 个知识文件,可在项目中减少或分拆。"
        msg += " 或调高 AGENT_MAX_REQUEST_BYTES、新建会话后再试。"
        return msg

    async def _degrade_request(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        provider: LLMProvider,
    ) -> str:
        """收缩超限的请求体，按破坏性最小的顺序依次尝试。

        在一轮开始时运行一次（在 force_compress_history 的同步截断之后）。
        下方三个异步/内容感知阶段，正是那个确定性截断无法安全完成的动作：

          1. 强制 LLM 摘要早期历史——捕获 Token 阈值漏掉的字节密集型历史
             （中日韩字符转义、图片）；
          2. 把最大的静态知识文件降级为溢出（不全文加载），并重建系统提示
             ——全文加载的知识是系统提示中唯一无法再压缩的大块；
          3. 把 base64 图片降级为路径引用——会损害视觉能力，放在最后。

        载荷一旦合规即返回 "ok"；所有阶段都跑完仍不合规则返回
        "over_limit"。本方法绝不抛出异常：每个阶段都是软预算杠杆，调用方
        仅在收到 "over_limit" 时才升级为硬性停止。
        """
        tools_wire = estimate_request_bytes([], tool_defs)
        if estimate_request_bytes(messages, tool_defs, tools_wire) <= MAX_REQUEST_BYTES:
            return "ok"

        # 1. 历史：强制摘要早期片段（本轮用户消息位于 messages[-1]，作为
        #    近期尾部保留）。
        from .compressor import RECENT_TURNS_KEEP
        if len(messages) - 1 > RECENT_TURNS_KEEP:
            try:
                system_msgs = [m for m in messages if m.role == "system"]
                history = [m for m in messages if m.role != "system"]
                # step5 可能已在本轮做过摘要；对摘要对再次摘要只会让上下文
                # 漂移，却换不来任何字节收益。
                already_summarized = bool(history) and (
                    isinstance(history[0].content, str)
                    and history[0].content.startswith(SUMMARY_MARKER)
                )
                reserved = estimate_history_tokens(system_msgs)
                available_budget = compression_budget(
                    provider.context_window, reserved, self._config.max_tokens
                )
                if available_budget > 0 and not already_summarized:
                    import asyncio as _asyncio
                    async with _asyncio.timeout(60):
                        compressed = await compress_history(
                            history, available_budget, provider, force=True
                        )
                    if compressed is not history:
                        messages[:] = system_msgs + compressed
                        _log.warning("request degrade: force-summarized early history")
            except Exception as e:
                _log.warning("request degrade: history summarization failed: %s", e, exc_info=True)
            if estimate_request_bytes(messages, tool_defs, tools_wire) <= MAX_REQUEST_BYTES:
                return "ok"

        # 2. 知识：先丢弃最大的静态文件，然后重建。
        if self._project_context and self._project_context.loaded_content:
            from .knowledge.loader import demote_loaded_entry
            demoted = 0
            while (
                estimate_request_bytes(messages, tool_defs, tools_wire) > MAX_REQUEST_BYTES
                and self._project_context.loaded_content
            ):
                largest = max(
                    self._project_context.loaded_content,
                    key=lambda s: len(self._project_context.loaded_content[s].encode("utf-8")),
                )
                if not demote_loaded_entry(self._project_context, largest):
                    break
                demoted += 1
                # 在循环内部重建：循环条件测量的是 messages[0]，而加载的
                # 内容位于缓存的静态提示词中。若仅在循环后重建，测量尺寸
                # 始终不变，导致所有加载文件都被降级，而非只降到刚好合规。
                self._invalidate_static_cache()
                messages[0] = Message(role="system", content=self._build_system_prompt())
            if demoted:
                _log.warning("request degrade: demoted %d knowledge files into overflow", demoted)
            if estimate_request_bytes(messages, tool_defs, tools_wire) <= MAX_REQUEST_BYTES:
                return "ok"

        # 3. 图片：最后手段——以牺牲视觉能力换取本轮能正常运行。
        from .compressor import demote_image_messages
        if demote_image_messages(messages):
            _log.warning("request degrade: demoted image messages to text references")
        if estimate_request_bytes(messages, tool_defs, tools_wire) <= MAX_REQUEST_BYTES:
            return "ok"

        return "over_limit"

    async def _run_loop(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        provider: LLMProvider,
        session: ConversationSession,
        config_id: str,
    ) -> None:
        """带 stop_reason 状态机的工具调用循环。"""
        # 纵深防御：恢复的历史（中断运行、压缩切片）可能包含缺少助手配对的
        # 工具消息；在此先做一次清洗，保证本轮每次供应方调用都格式良好。
        messages = self._drop_orphan_tool_messages(messages)
        message_id = session.new_message()
        max_iterations = _MAX_CHAT_ITERATIONS
        max_pause_continuations = 3
        pause_count = 0
        emitted_end = False
        # 字节守卫常量：工具模式在一次循环内是静态的，逐次迭代的守卫不应
        # 每一步都重新估算它们。
        tools_wire = estimate_request_bytes([], tool_defs)
        # 每循环的提示词版本号——每个循环跟踪自己上次构建的版本；任何并行
        # 任务做出改动都会推高这个共享版本号。
        prompt_revision = self._prompt_revision

        try:
            budget = max_iterations
            # 安全阀：注入消息会刷新 budget，但绝不会刷新 total_budget，因此
            # 病态的注入消息流也无法让循环永远运行下去。
            total_budget = max_iterations * 5

            async def _maybe_inject() -> bool:
                """在步骤边界消费排队的用户输入，并刷新迭代预算——一次注入
                就是一条新的用户指令。"""
                nonlocal message_id, budget
                if session.is_aborted():
                    return False
                new_id = await self._consume_injected_input(
                    session, messages, provider, message_id
                )
                if new_id is None:
                    return False
                message_id = new_id
                budget = max_iterations
                return True

            exhausted = False
            while True:
                if session.is_aborted():
                    await session.emit("abort_ack", message_id=message_id)
                    # 持久化已累积的内容，使中断的运行在重新加载后仍可见于
                    # 历史——硬取消路径已通过下方 finally 定稿，优雅中止也
                    # 必须如此，否则部分消息会消失。
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="aborted")
                    emitted_end = True
                    break

                if budget <= 0 or total_budget <= 0:
                    # 预算耗尽。自上次检查以来到达的注入（如工具执行期间）
                    # 会刷新预算；否则循环结束。total_budget 是硬安全阀，
                    # 永不刷新。
                    if budget <= 0 and await _maybe_inject():
                        continue
                    exhausted = True
                    break

                budget -= 1
                total_budget -= 1
                iteration = max_iterations - budget

                if await _maybe_inject():
                    continue

                if self._prompt_revision != prompt_revision:
                    messages[0] = Message(role="system", content=self._build_system_prompt())
                    prompt_revision = self._prompt_revision

                # 流式接收响应
                full_text = ""
                tool_calls_buffer: dict[int, dict] = {}
                preparing_emitted: set[int] = set()
                stop_reason: StopReason = StopReason.UNKNOWN
                # 本次迭代的扩展思考（extended-thinking）累积。带签名的块会
                # 随下一次工具循环请求放在助手消息上；无签名文本仅用于展示。
                thinking_text = ""
                thinking_blocks: list[dict] = []
                thinking_open = False
                phase_emitted: set[str] = set()
                responding_emitted = False

                history_summary, pending_tool_ids = self._history_tool_call_summary(messages)
                _log.info(
                    "_run_loop iter=%d provider=%s messages=%d history=%s pending_tool_call_ids=%s: calling provider.stream",
                    iteration,
                    provider.model_name,
                    len(messages),
                    history_summary,
                    pending_tool_ids,
                )
                if self._request_byte_outcome(messages, tool_defs, tools_wire) == "over_limit":
                    # 同步截断（工具输出）仍不够。升级到异步降级链——强制
                    # 摘要历史、降级最大的知识文件、再剥离图片——然后才带着
                    # 可操作的错误放弃，而不是让网关以含义不明的 413 拒绝。
                    await session.emit("turn_status", phase="degrading", message_id=message_id)
                    if await self._degrade_request(messages, tool_defs, provider) == "over_limit":
                        await session.emit("error", message=self._over_limit_message(messages, tool_defs))
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="api_error")
                        emitted_end = True
                        break
                    _log.info("request degrade succeeded, resuming turn")
                try:
                    # 阶段通知覆盖首 Token 延迟（TTFT）：供应方应答前没有任何
                    # 数据流，否则前端只会显示旧的跳动圆点。
                    await session.emit("turn_status", phase="waiting_model", message_id=message_id)
                    # OpenAI 兼容网关以请求累计总量上报用量，并可能在每个
                    # 数据块重复携带（vLLM 运行统计）。记录本调用自上次快照
                    # 以来的增量，避免 tokens_used 被数据块数量虚高。
                    prev_usage: tuple[int, int] = (0, 0)
                    async for chunk in provider.stream(
                        messages, tool_defs, max_tokens=self._config.max_tokens
                    ):
                        if chunk.thinking:
                            if not thinking_open:
                                thinking_open = True
                                thinking_text = ""
                                if "thinking" not in phase_emitted:
                                    phase_emitted.add("thinking")
                                    await session.emit("turn_status", phase="thinking", message_id=message_id)
                            thinking_text += chunk.thinking
                            await session.emit("thinking_delta", delta=chunk.thinking, message_id=message_id)
                        elif chunk.thinking_signature is not None:
                            # 带签名块结束：保留它供下一次工具循环请求使用
                            # （Anthropic 会把它原样回传）。
                            if thinking_open:
                                if thinking_text and chunk.thinking_signature:
                                    thinking_blocks.append({
                                        "type": "thinking",
                                        "thinking": thinking_text,
                                        "signature": chunk.thinking_signature,
                                    })
                                thinking_text = ""
                                thinking_open = False
                                await session.emit("thinking_end", message_id=message_id)
                        elif (chunk.delta or chunk.tool_call_delta) and thinking_open:
                            # 从不发送签名的网关：首个真实内容即关闭这个仅
                            # 用于展示的块。
                            thinking_text = ""
                            thinking_open = False
                            await session.emit("thinking_end", message_id=message_id)

                        if chunk.delta:
                            if not responding_emitted:
                                responding_emitted = True
                                await session.emit("turn_status", phase="responding", message_id=message_id)
                            full_text += chunk.delta
                            await session.emit("stream_delta", delta=chunk.delta, message_id=message_id)

                        if chunk.tool_call_delta:
                            idx = chunk.tool_call_delta.get("index", 0)
                            is_new = idx not in tool_calls_buffer
                            if is_new:
                                tool_calls_buffer[idx] = {"id": chunk.tool_call_delta.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
                            tc = tool_calls_buffer[idx]
                            new_id = chunk.tool_call_delta.get("id", "")
                            if new_id:
                                tc["id"] = new_id
                            fn = chunk.tool_call_delta.get("function", {})
                            if fn.get("name"):
                                tc["function"]["name"] = fn["name"]
                            if idx not in preparing_emitted and tc["id"] and tc["function"]["name"]:
                                preparing_emitted.add(idx)
                                await session.emit("tool_call_preparing", tool=tc["function"]["name"], call_id=tc["id"])
                                await session.emit("turn_status", phase="running_tool", tool=tc["function"]["name"], call_id=tc["id"], message_id=message_id)
                            current_args = tc["function"].get("arguments", "")
                            tc["function"]["arguments"] = self._merge_tool_args(current_args, fn.get("arguments", ""))

                        if chunk.stop_reason:
                            stop_reason = chunk.stop_reason

                        if chunk.usage:
                            d_prompt, d_completion, prompt_tokens, completion_tokens = _consume_usage(
                                chunk.usage, prev_usage
                            )
                            session.add_usage(
                                prompt_tokens=d_prompt,
                                completion_tokens=d_completion,
                            )
                            prev_usage = (
                                max(prev_usage[0], prompt_tokens),
                                max(prev_usage[1], completion_tokens),
                            )
                            session.context_tokens = prompt_tokens + completion_tokens
                            session.context_window = provider.context_window
                            await session.emit(
                                "token_update",
                                used=session.tokens_used,
                                budget=session.token_budget,
                                context_tokens=session.context_tokens,
                                context_window=session.context_window,
                            )
                except Exception as api_err:
                    # SDK/h11 异常会回显凭证（"Incorrect API key provided:
                    # sk-..."、"Illegal header value b'...'"）——在字符串进入
                    # 发出的事件或日志前先脱敏（exc_info 也会打印原始异常）。
                    err_type = type(api_err).__name__
                    err_msg = (
                        f"LLM API error: {_scrub_credentials(str(api_err), self._credentials)}\n"
                        f"  model={provider.model_name}, iteration={iteration}, "
                        f"messages={len(messages)}, error_type={err_type}"
                    )
                    if pending_tool_ids:
                        err_msg += f", pending_tool_call_ids={pending_tool_ids}"
                    _log.error(
                        "provider.stream() failed: provider=%s iter=%d messages=%d pending_tool_call_ids=%s error=%s",
                        provider.model_name,
                        iteration,
                        len(messages),
                        pending_tool_ids,
                        _scrub_credentials(traceback.format_exc(), self._credentials),
                    )
                    if classify_api_error(api_err) is StopReason.CONTEXT_EXCEEDED:
                        # OpenAI/Gemini/网关把上下文溢出作为普通 400/413 异常
                        # 抛出（只有 Anthropic 以停止原因流式返回）——将其路由
                        # 到同一个 context_exceeded 处理，而非原始的"LLM API
                        # error"气泡。
                        await session.emit("context_exceeded", message_id=message_id, config_id=config_id)
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="context_exceeded")
                        emitted_end = True
                        break
                    await session.emit("error", message=err_msg)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="api_error")
                    emitted_end = True
                    break

                # 流结束时思考块仍开着（既无签名也无后续内容）——关闭该块，
                # 让前端把它折叠起来。
                if thinking_open:
                    thinking_open = False
                    await session.emit("thinking_end", message_id=message_id)

                # 把助手消息追加到历史。在工具调用中途被截断的流
                # （finish_reason="length"、pause_turn、连接断开）可能留下
                # id/name 始终未到达的条目——持久化它们会让 tool_call 缺少
                # 匹配的 tool_result 而成为孤儿，下一次供应方请求会因此返回
                # 400。在此丢弃不完整条目（MAX_TOKENS/END_TURN 随后会落到
                # 下方纯文本路径）。
                tool_calls = (
                    [tc for tc in tool_calls_buffer.values()
                     if tc.get("id") and tc.get("function", {}).get("name")]
                    if tool_calls_buffer else None
                ) or None
                assistant_msg = Message(
                    role="assistant", content=full_text, tool_calls=tool_calls,
                    thinking_blocks=thinking_blocks or None,
                )
                messages.append(assistant_msg)

                # ─── 停止原因（Stop Reason）状态机 ────────────────────────
                if stop_reason == StopReason.REFUSAL:
                    await session.emit("refusal", message_id=message_id, content=full_text)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="refusal")
                    emitted_end = True
                    break

                if stop_reason == StopReason.CONTEXT_EXCEEDED:
                    await session.emit("context_exceeded", message_id=message_id, config_id=config_id)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="context_exceeded")
                    emitted_end = True
                    break

                if stop_reason == StopReason.CONTENT_FILTER:
                    await session.emit("content_filtered", message_id=message_id)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="content_filter")
                    emitted_end = True
                    break

                if stop_reason == StopReason.MAX_TOKENS:
                    if not tool_calls:
                        if await _maybe_inject():
                            continue
                        await session.emit("warning", message="Response truncated (max_tokens reached)")
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="max_tokens")
                        emitted_end = True
                        break

                if stop_reason == StopReason.PAUSE_TURN:
                    pause_count += 1
                    if pause_count > max_pause_continuations:
                        if await _maybe_inject():
                            # 预算耗尽属于注入前的对话；新的用户消息会
                            # 开启新一轮，否则下一次合法 pause 会立即结束
                            # 本轮。
                            pause_count = 0
                            continue
                        await session.emit("warning", message="Too many pause_turn continuations, stopping")
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="pause_turn_exhausted")
                        emitted_end = True
                        break
                    # 不做工具处理直接继续——模型在生成中途暂停了
                    if not tool_calls:
                        if await _maybe_inject():
                            pause_count = 0
                            continue
                        continue

                if stop_reason in (StopReason.END_TURN, StopReason.UNKNOWN) and not tool_calls:
                    if await _maybe_inject():
                        continue
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used)
                    emitted_end = True
                    break

                if not tool_calls:
                    if await _maybe_inject():
                        continue
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used)
                    emitted_end = True
                    break

                # ─── 处理工具调用（Process Tool Calls）────────────────────
                pause_count = 0  # 工具使用周期成功时重置

                # 进入任务模式前先完成普通调用。嵌套任务请求会继承这个助手
                # 轮次，因此每个同级调用的历史中都必须已有匹配的工具响应。
                for tc in sorted(
                    tool_calls,
                    key=lambda item: item.get("function", {}).get("name", "") == "plan_task",
                ):
                    tool_name = tc.get("function", {}).get("name", "")
                    tool_id = tc.get("id")
                    if not tool_name or not tool_id:
                        continue
                    try:
                        args = self._parse_tool_arguments(tc)
                    except (json.JSONDecodeError, TypeError) as _e:
                        # 保持助手工具调用可被下一次供应方请求序列化；
                        # Anthropic 会重新解析该字段。
                        tc["function"]["arguments"] = "{}"
                        bad_result = {"error": f"Malformed tool arguments (invalid JSON): {_e}"}
                        await session.emit("tool_call_start", tool=tool_name, input={}, call_id=tool_id)
                        await session.emit("turn_status", phase="running_tool", tool=tool_name, call_id=tool_id, message_id=message_id)
                        await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(bad_result), call_id=tool_id)
                        messages.append(Message(role="tool", content=_dumps(bad_result), tool_call_id=tool_id, name=tool_name))
                        continue

                    await session.emit("tool_call_start", tool=tool_name, input=args, call_id=tool_id)
                    await session.emit("turn_status", phase="running_tool", tool=tool_name, call_id=tool_id, message_id=message_id)

                    if tool_name == "plan_task":
                        try:
                            task_summary = await self._run_task_mode(
                                args, provider, session, messages, tool_defs, config_id, tool_id
                            )
                            result = {"status": "completed", "summary": task_summary}
                        except asyncio.CancelledError:
                            result = {"error": "Tool execution interrupted"}
                            await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(result), call_id=tool_id, status="error")
                            raise
                        except Exception as e:
                            # 与下方普通工具路径相同的降级：格式错误的计划
                            # （tasks 不是列表、条目缺 id/title）必须作为模型
                            # 能看到并自我纠正的工具错误暴露，而不是让整轮
                            # 崩溃成通用的"Agent execution failed"。
                            _log.warning("plan_task failed (call_id=%s): %s", tool_id, e, exc_info=True)
                            # _run_task_mode 可能在设置 _current_task_id /
                            # _task_plan 之后才失败——重置它们，避免陈旧任务
                            # ID 下一轮被重新注入共享工具上下文（动态知识会
                            # 绑定到一个永不完成的任务并泄漏），也让下方快照
                            # 不渲染卡住的任务。
                            self._current_task_id = None
                            self._tool_ctx.current_task_id = None
                            for tp in self._task_plan or []:
                                if tp["status"] in ("pending", "running"):
                                    tp["status"] = "failed"
                                    tp["error"] = "plan_task failed"
                            result = {"error": f"plan_task failed: {str(e)[:500]}"}
                        # 把最终计划快照附到工具输出，使已完成消息能在历史中
                        # 渲染计划。下方回给 LLM 的结果保持精简（不膨胀上下文）。
                        plan_snapshot = [
                            {
                                "id": t["id"],
                                "title": t["title"],
                                "status": t["status"],
                                **({"summary": t["summary"]} if t.get("summary") else {}),
                                **({"error": t["error"]} if t.get("error") else {}),
                            }
                            for t in getattr(self, "_task_plan", None) or []
                        ]
                        await session.emit(
                            "tool_call_end",
                            tool=tool_name,
                            output=_clip_tool_output({**result, "tasks": plan_snapshot}),
                            call_id=tool_id,
                        )
                        messages.append(Message(
                            role="tool",
                            content=_dumps(result),
                            tool_call_id=tool_id,
                            name=tool_name,
                        ))
                        continue

                    tool = self._tools.get(tool_name)
                    if tool is None:
                        result = {"error": f"Unknown tool: {tool_name}"}
                    else:
                        try:
                            result = await tool.execute(args, self._tool_ctx)
                        except asyncio.CancelledError:
                            result = {"error": "Tool execution interrupted"}
                            await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(result), call_id=tool_id, status="error")
                            raise
                        except Exception as e:
                            _log.warning("Tool %s failed (call_id=%s): %s", tool_name, tool_id, e, exc_info=True)
                            result = {"error": str(e)[:500]}

                    await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(result), call_id=tool_id)

                    # 校验类型而非仅真假值：有些工具在这些键下报告的是数量
                    # （repo_graph 的 too_many_files 摘要带有 files=<整数>），
                    # 而在此处遍历整数——位于工具 try/except 之外——会以
                    # "'int' object is not iterable" 让整轮崩溃。
                    images = result.get("images")
                    if isinstance(images, list) and images:
                        await session.emit("image_output", message_id=message_id, images=images)

                    out_files = result.get("files")
                    if isinstance(out_files, list) and out_files:
                        _deliverable = [f for f in out_files if isinstance(f, dict) and f.get("url")]
                        if _deliverable:
                            await session.emit("file_output", message_id=message_id, files=_deliverable)

                    if tool_name == "knowledge_rw" and self._project_context:
                        op = args.get("operation")
                        if op == "write" and not result.get("error"):
                            from .knowledge.loader import update_context_file, _is_log_role
                            slug = args.get("slug", "")
                            content = args.get("content", "")
                            if slug and content and not _is_log_role(content):
                                update_context_file(self._project_context, slug, content)
                                self._invalidate_static_cache()
                                self._mark_prompt_dirty()
                                await session.emit("knowledge_updated")
                        elif op in ("load", "unload", "refresh", "delete", "purge"):
                            # delete/purge 会移除文件或索引行——提示词的知识
                            # 列表必须重建，否则 Agent 会持续围绕已删除文件
                            # 推理。静态缓存原样内嵌了 ctx.loaded_content，而
                            # 脏标志触发的重建在缓存存在时会直接返回该缓存——
                            # 仅靠脏标志会重新提供已删除文件的内容。
                            self._invalidate_static_cache()
                            self._mark_prompt_dirty()

                    if tool_name == "memory_rw":
                        op = args.get("operation")
                        if op in ("save", "update", "archive"):
                            self._invalidate_static_cache()
                            self._mark_prompt_dirty()

                    messages.append(Message(
                        role="tool",
                        content=_dumps(result),
                        tool_call_id=tool_id,
                        name=tool_name,
                    ))
            if exhausted:
                if not emitted_end:
                    await session.emit("warning", message="Reached maximum loop iterations - reply to continue")
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="max_iterations")
                    emitted_end = True
        finally:
            if not emitted_end:
                # 硬取消（流式或工具中途停止）会直接展开到这个兜底，而它
                # 过去发出时不带 stop_reason——API 随后把运行归类为
                # "completed"，又因为 finish_run 只会从 "running" 状态转出，
                # 之后的 finish_run(..., "interrupted") 变成空操作。表现为
                # "停止、重新加载后，运行却显示已完成"。
                await session.emit(
                    "stream_end",
                    message_id=message_id,
                    total_tokens=session.tokens_used,
                    **({"stop_reason": "aborted"} if session.is_aborted() else {}),
                )
            await self._tool_ctx.cleanup()
            await session.close()

    def _compute_task_progress(self, current_task_id: str | None = None) -> dict[str, Any]:
        """从实时任务计划推导进度信息（可感知并行）。"""
        if not self._task_plan:
            return {}
        completed = sum(1 for t in self._task_plan if t["status"] == "completed")
        total = len(self._task_plan)

        running_tasks = [t for t in self._task_plan if t["status"] == "running"]
        if len(running_tasks) == 1:
            current_title: str | None = running_tasks[0]["title"]
        elif len(running_tasks) > 1:
            current_title = f"{len(running_tasks)} 个任务并行运行中"
        else:
            current_title = None

        # 下一步：首个依赖全部完成的待处理（pending）任务
        completed_ids = {t["id"] for t in self._task_plan if t["status"] == "completed"}
        next_title: str | None = None
        for t in self._task_plan:
            if t["status"] == "pending":
                dep_ids = set(t.get("depends_on") or [])
                if dep_ids <= completed_ids:
                    next_title = t["title"]
                    break

        return {
            "current_step": current_title,
            "next_step": next_title,
            "progress_completed": completed,
            "progress_total": total,
        }

    async def _emit_task_progress(self, session: ConversationSession, task_id: str) -> None:
        """携带当前计划状态发出 task_progress（任务进度）事件。"""
        progress = self._compute_task_progress(task_id)
        if progress:
            await session.emit("task_progress", task_id=task_id, **progress)

    async def _emit_task_stream_end(
        self,
        session: ConversationSession,
        task_tool_ctx: ToolContext,
        task_id: str,
    ) -> None:
        """在失败/中止路径后，关闭前端上该任务的流。

        _run_task_loop 在正常返回路径上会发出 stream_end，但超时/API 错误/
        拒绝会绕过它们——而前端只在收到 stream_end 时才停止该任务的输入
        指示器，而非 task_failed。若失败发生在 _run_task_loop 打开其流之前
        （还没有 message_id），说明从未开始任何东西——此时不发任何事件，
        而不是发出 message_id=None 的 stream_end。
        """
        message_id = getattr(task_tool_ctx, "current_message_id", None)
        if message_id is None:
            return
        try:
            await session.emit(
                "stream_end",
                message_id=message_id,
                total_tokens=session.tokens_used,
                task_id=task_id,
            )
        except Exception:
            _log.debug("stream_end emit failed after task failure", exc_info=True)

    async def _run_task_mode(
        self,
        plan_args: dict,
        provider: LLMProvider,
        session: ConversationSession,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        config_id: str,
        plan_tool_id: str,
    ) -> str:
        """以 DAG 感知的并行调度执行规划好的任务列表。

        无依赖的任务并发运行（由信号量限流）。任务完成后，检查其依赖方，
        若就绪则派发。失败任务会导致其依赖方被跳过。

        返回摘要字符串。
        """
        tasks = self._normalize_task_plan(plan_args.get("tasks", []))
        await session.emit("task_plan_created", tasks=tasks)

        # 为动态提示词初始化实时任务计划
        self._task_plan = [
            {
                "id": t["id"],
                "title": t["title"],
                "status": "pending",
                "depends_on": t.get("depends_on") or [],
            }
            for t in tasks
        ]
        self._mark_prompt_dirty()

        if not tasks:
            summary = "No tasks to execute."
            await session.emit("agentic_loop_completed", tasks_done=0, tasks_failed=0, summary=summary)
            return summary

        # 构建 DAG；检测到环时回退为顺序执行
        try:
            deps, dependents = self._build_dependency_graph(tasks)
        except ValueError:
            _log.warning("Task dependency cycle detected, falling back to sequential execution")
            await session.emit("warning", message="Task dependency cycle detected, falling back to sequential execution")
            # 顺序回退：按顺序把所有任务串成一条链
            deps = {}
            dependents = {str(t["id"]): set() for t in tasks}
            task_id_list = [str(t["id"]) for t in tasks]
            for i in range(len(task_id_list) - 1):
                deps.setdefault(task_id_list[i + 1], set()).add(task_id_list[i])
                dependents[task_id_list[i]].add(task_id_list[i + 1])

        task_map = {str(t["id"]): t for t in tasks}
        completed: set[str] = set()
        failed: set[str] = set()
        task_results: dict[str, str] = {}  # 任务 ID -> 摘要文本
        semaphore = asyncio.Semaphore(3)  # 最大并发数

        # 调度状态
        dispatched: set[str] = set()  # 已开始或已完成的任务
        pending_futures: set[asyncio.Task] = set()

        async def _cascade_skip(task_id: str) -> None:
            """把 *task_id* 的所有传递性依赖方标记为已跳过。"""
            for dep_tid in dependents.get(task_id, set()):
                if dep_tid in completed or dep_tid in failed or dep_tid in dispatched:
                    continue
                failed.add(dep_tid)
                dispatched.add(dep_tid)
                for tp in self._task_plan:
                    if tp["id"] == dep_tid:
                        tp["status"] = "skipped"
                        tp["error"] = "Skipped: dependency failed"
                        break
                self._mark_prompt_dirty()
                await session.emit("task_skipped", task_id=dep_tid, error="Skipped: dependency failed")
                task_results[dep_tid] = f"[SKIPPED] {task_map[dep_tid]['title']}"
                await _cascade_skip(dep_tid)

        async def _dispatch_ready() -> None:
            """启动所有依赖已全部完成的任务。"""
            for t in tasks:
                tid = str(t["id"])
                if tid in dispatched:
                    continue
                # 注入（或中止）会停止新的派发；在途任务自然结束，下方循环
                # 退出。
                if session.is_aborted() or session.has_pending_user_input():
                    break
                task_deps = deps.get(tid, set())
                if not task_deps or task_deps <= completed:
                    dispatched.add(tid)
                    fut = asyncio.create_task(_execute_and_finish(tid))
                    pending_futures.add(fut)
                    # 完成后移除自引用，避免保留大型闭包
                    fut.add_done_callback(pending_futures.discard)

        async def _execute_and_finish(task_id: str) -> None:
            """执行单个任务，然后派发新就绪的依赖方。"""
            async with semaphore:
                if session.is_aborted():
                    # 尚未开始则标记为已跳过
                    failed.add(task_id)
                    for tp in self._task_plan:
                        if tp["id"] == task_id:
                            tp["status"] = "skipped"
                            tp["error"] = "Aborted"
                            break
                    self._mark_prompt_dirty()
                    await session.emit("task_skipped", task_id=task_id, error="Aborted")
                    return
                try:
                    dep_results = [
                        (task_map[dep]["title"], task_results[dep])
                        for dep in sorted(deps.get(task_id, set()))
                        if dep in task_results
                    ]
                    result = await self._execute_single_task(
                        task_map[task_id], provider, session, messages,
                        tool_defs, config_id, plan_tool_id,
                        dependency_results=dep_results,
                    )
                except Exception as e:
                    # 崩溃不能让任务卡在 "running"，使其依赖方永远无法派发
                    # （带 return_exceptions=True 的 gather 会静默吞掉它）。
                    # 对整条回溯脱敏：即使异常自身消息在抛出点已脱敏，其
                    # __cause__ 链仍可能携带原始供应方错误。
                    scrubbed = _scrub_credentials(str(e), self._credentials)
                    _log.error(
                        "Task %s crashed: %s",
                        task_id,
                        _scrub_credentials(traceback.format_exc(), self._credentials),
                    )
                    for tp in self._task_plan:
                        if tp["id"] == task_id:
                            tp["status"] = "failed"
                            tp["error"] = f"Execution error: {scrubbed}"[:500]
                            break
                    self._mark_prompt_dirty()
                    await session.emit("task_failed", task_id=task_id, error=f"Execution error: {scrubbed}"[:500])
                    failed.add(task_id)
                    task_results[task_id] = f"[FAILED] {task_map[task_id]['title']}"
                    await _cascade_skip(task_id)
                    if not session.is_aborted():
                        await _dispatch_ready()
                    return

            status = result["status"]
            task_results[task_id] = result["summary"]

            if status == "completed":
                completed.add(task_id)
            else:
                failed.add(task_id)

            # 检查依赖方
            if status != "completed":
                # 失败 -> 级联跳过依赖方
                await _cascade_skip(task_id)

            # 派发新就绪的任务（除非已中止）
            if not session.is_aborted():
                await _dispatch_ready()

        # ── 初始派发 ──────────────────────────────────────────────
        await _dispatch_ready()

        # ── 等待所有任务结束 ─────────────────────────────────────
        # 任务是动态派发的（每次完成都可能调度其依赖方），因此必须循环：
        # 单个 gather() 只快照调用时刻的集合，会在新派发任务仍运行时就返回。
        while pending_futures:
            await asyncio.gather(*pending_futures, return_exceptions=True)

        # 在途注入：用户在任务运行期间排队了一条消息。在此停止（剩余任务
        # 保持 pending——处理器的陈旧任务清扫会跳过其 ID），把计划保留在
        # 动态提示词中，并把控制权交回 _run_loop，由其步骤边界消费排队
        # 消息。延迟的任务不标记为已跳过：用户可能用新指令恢复它们。
        if session.has_pending_user_input():
            deferred = [
                str(t["id"]) for t in tasks
                if str(t["id"]) not in completed and str(t["id"]) not in failed
            ]
            summary = (
                f"Completed {len(completed)}/{len(tasks)} tasks. "
                "User interrupted; remaining tasks kept pending."
            )
            await session.emit(
                "agentic_loop_completed",
                tasks_done=len(completed),
                tasks_failed=len(failed),
                summary=summary[:500],
                interrupted=True,
                deferred_task_ids=deferred,
            )
            self._current_task_id = None
            self._tool_ctx.current_task_id = None
            return summary

        # 中止：把任何剩余的待处理任务标记为已跳过
        if session.is_aborted():
            for t in tasks:
                tid = str(t["id"])
                if tid not in completed and tid not in failed:
                    failed.add(tid)
                    for tp in self._task_plan:
                        if tp["id"] == tid:
                            tp["status"] = "skipped"
                            tp["error"] = "Aborted"
                            break
                    await session.emit("abort_ack", task_id=tid)
                    await session.emit("task_skipped", task_id=tid, error="Aborted")
                    task_results.setdefault(tid, f"[ABORTED] {t['title']}")

        self._current_task_id = None
        self._tool_ctx.current_task_id = None

        tasks_done = len(completed)
        tasks_failed_count = len(failed)
        summaries = [task_results.get(str(t["id"]), f"[?] {t['title']}") for t in tasks]
        summary = f"Completed {tasks_done}/{len(tasks)} tasks.\n" + "\n".join(summaries)
        await session.emit("agentic_loop_completed", tasks_done=tasks_done, tasks_failed=tasks_failed_count, summary=summary[:500])
        return summary

    async def _execute_single_task(
        self,
        task: dict,
        provider: LLMProvider,
        session: ConversationSession,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        config_id: str,
        plan_tool_id: str,
        dependency_results: list[tuple[str, str]] | None = None,
    ) -> dict:
        """执行单个规划任务，返回 {task_id, status, summary}。"""
        from .knowledge.loader import unload_by_task

        task_id = task["id"]
        self._turn += 1
        turn = self._turn
        task_tool_ctx = self._tool_ctx.copy_for_task(task_id, turn)
        # 自动路由模式：选择服务于该任务复杂度层级的配置（最近层级回退）。
        # 显式模式则沿用会话配置。
        task_config = config_id
        if self._tier_map:
            resolved = resolve_tier_config(self._tier_map, task.get("estimated_complexity"))
            if resolved and resolved in self._tier_models:
                task_config = resolved
        task_provider = self._get_provider(task_config)

        # 更新实时计划状态
        for tp in self._task_plan:
            if tp["id"] == task_id:
                tp["status"] = "running"
                break
        self._mark_prompt_dirty()

        progress = self._compute_task_progress(task_id)
        await session.emit(
            "task_started",
            task_id=task_id,
            title=task["title"],
            model_tier=task_config,
            actual_model=task_provider.model_name,
            **progress,
        )

        task_messages = self._build_task_messages(
            messages, plan_tool_id, task["title"], dependency_results
        )
        task_history, pending_task_tool_ids = self._history_tool_call_summary(task_messages)
        _log.info(
            "task execution starting: task_id=%s plan_tool_id=%s provider=%s messages=%d history=%s pending_tool_call_ids=%s",
            task_id,
            plan_tool_id,
            task_provider.model_name,
            len(task_messages),
            task_history,
            pending_task_tool_ids,
        )

        async def _close_task_db() -> None:
            """释放 copy_for_task 创建的单任务数据库会话（若有）。

            处理器拥有的共享 tool_db 在那里关闭；单任务会话必须在此关闭，
            避免并发任务泄漏连接池槽位。
            """
            _sess = task_tool_ctx.db_session
            if _sess is not None and _sess is not self._tool_ctx.db_session:
                try:
                    await _sess.close()
                except Exception:
                    _log.debug("Failed to close per-task db session", exc_info=True)

        timeout_s = _task_timeout_seconds()
        try:
            async with asyncio.timeout(timeout_s):
                full_text = await self._run_task_loop(
                    task_messages, tool_defs, task_provider, session,
                    task_id=task_id, turn=turn, task_tool_ctx=task_tool_ctx,
                )
        except TimeoutError:
            error_text = f"Task timed out ({timeout_s}s)"
            await session.emit("task_failed", task_id=task_id, error=error_text)
            await self._emit_task_stream_end(session, task_tool_ctx, task_id)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "failed"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "failed", "summary": f"[TIMEOUT] {task['title']}"}
        except Exception as e:
            error_text = str(e)[:500]
            await session.emit("task_failed", task_id=task_id, error=error_text)
            await self._emit_task_stream_end(session, task_tool_ctx, task_id)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "failed"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "failed", "summary": f"[ERROR] {task['title']}: {e}"}
        finally:
            await _close_task_db()

        # 循环中途触发的中止不能把在途任务报告为已完成——_run_task_mode 的
        # 中止块把剩余待处理任务标记为 "skipped"，此处保持相同语义。否则该
        # 任务的完成事件会被持久化，其结果显示为已完成。这里不发
        # stream_end：_run_task_loop 在正常返回路径（含中止 break）会发出，
        # 用相同 message_id 再发一次会让前端流被定稿两次。
        if session.is_aborted():
            error_text = "Aborted"
            await session.emit("task_skipped", task_id=task_id, error=error_text)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "skipped"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "skipped", "summary": f"[ABORTED] {task['title']}"}

        # 成功
        await session.emit("task_completed", task_id=task_id, summary=full_text[:500])
        for tp in self._task_plan:
            if tp["id"] == task_id:
                tp["status"] = "completed"
                tp["summary"] = full_text[:500]
                break
        self._mark_prompt_dirty()

        # 任务状态变更后发出更新后的进度
        await self._emit_task_progress(session, task_id)

        # 释放绑定到本任务的动态知识
        if self._project_context:
            unloaded = unload_by_task(self._project_context, task_id)
            if unloaded:
                await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                self._mark_prompt_dirty()

        return {"task_id": task_id, "status": "completed", "summary": f"[DONE] {task['title']}: {full_text[:200]}"}

    async def _run_task_loop(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        provider: LLMProvider,
        session: ConversationSession,
        *,
        task_id: str,
        turn: int,
        task_tool_ctx: ToolContext,
    ) -> str:
        """单个任务的工具调用循环，处理 stop_reason，返回最终文本。

        使用 *task_tool_ctx*（单任务的浅拷贝），避免并发任务互相覆盖彼此的
        ``current_task_id`` / ``current_turn``。
        """
        max_iterations = _MAX_TASK_ITERATIONS
        max_pause_continuations = 3
        pause_count = 0
        message_id = session.new_message()
        # _run_task_loop 下方的失败路径（超时、API 错误、拒绝）仍须关闭前端
        # 上的流——仅 task_failed 会让输入指示器卡住。把 ID 暴露在单任务
        # 上下文上，使 _run_task 能在其 except 分支发出 stream_end
        # （task_tool_ctx 是单任务浅拷贝，不会跨任务覆盖）。
        task_tool_ctx.current_message_id = message_id
        full_text = ""
        # 本任务循环更早迭代产生的文本——每次迭代都会重置 full_text，但任务
        # 摘要（task_completed）必须反映整个任务，而非仅最后一轮。
        full_text_total: list[str] = []
        # 字节守卫常量：工具模式在一次循环内是静态的，逐次迭代的守卫不应
        # 每一步都重新估算它们。
        tools_wire = estimate_request_bytes([], tool_defs)
        # 每循环的提示词版本号（见 _run_loop）——并行任务在共享版本号推高时
        # 各自重建自己的系统提示词。
        prompt_revision = self._prompt_revision

        for _ in range(max_iterations):
            if session.is_aborted():
                break

            if self._prompt_revision != prompt_revision:
                messages[0] = Message(role="system", content=self._build_system_prompt())
                prompt_revision = self._prompt_revision

            full_text = ""
            tool_calls_buffer: dict[int, dict] = {}
            stop_reason: StopReason = StopReason.UNKNOWN
            # 与对话循环相同的扩展思考累积——任务迭代也会在下一次请求回放
            # 其带签名块。
            thinking_text = ""
            thinking_blocks: list[dict] = []
            thinking_open = False
            phase_emitted: set[str] = set()
            responding_emitted = False

            task_history, pending_tool_ids = self._history_tool_call_summary(messages)
            _log.info(
                "_run_task_loop task_id=%s provider=%s messages=%d history=%s pending_tool_call_ids=%s: calling provider.stream",
                task_id,
                provider.model_name,
                len(messages),
                task_history,
                pending_tool_ids,
            )
            if self._request_byte_outcome(messages, tool_defs, tools_wire) == "over_limit":
                # 同步截断不够。先升级到异步降级链；任务循环通过 RuntimeError
                # 暴露真正的超限（由 _run_task → task_failed →
                # _emit_task_stream_end 捕获），因此只在所有软杠杆都用尽后
                # 才抛出。
                await session.emit("turn_status", phase="degrading", task_id=task_id)
                if await self._degrade_request(messages, tool_defs, provider) == "over_limit":
                    raise RuntimeError(self._over_limit_message(messages, tool_defs))
                _log.info("request degrade succeeded in task loop, resuming")
            try:
                await session.emit("turn_status", phase="waiting_model", task_id=task_id)
                # 每次调用的累计用量快照——参见对话循环。
                prev_usage: tuple[int, int] = (0, 0)
                async for chunk in provider.stream(
                    messages, tool_defs, max_tokens=self._config.max_tokens
                ):
                    if chunk.thinking:
                        if not thinking_open:
                            thinking_open = True
                            thinking_text = ""
                            if "thinking" not in phase_emitted:
                                phase_emitted.add("thinking")
                                await session.emit("turn_status", phase="thinking", message_id=message_id, task_id=task_id)
                        thinking_text += chunk.thinking
                        await session.emit("thinking_delta", delta=chunk.thinking, message_id=message_id, task_id=task_id)
                    elif chunk.thinking_signature is not None:
                        if thinking_open:
                            if thinking_text and chunk.thinking_signature:
                                thinking_blocks.append({
                                    "type": "thinking",
                                    "thinking": thinking_text,
                                    "signature": chunk.thinking_signature,
                                })
                            thinking_text = ""
                            thinking_open = False
                            await session.emit("thinking_end", message_id=message_id, task_id=task_id)
                    elif (chunk.delta or chunk.tool_call_delta) and thinking_open:
                        thinking_text = ""
                        thinking_open = False
                        await session.emit("thinking_end", message_id=message_id, task_id=task_id)

                    if chunk.delta:
                        if not responding_emitted:
                            responding_emitted = True
                            await session.emit("turn_status", phase="responding", message_id=message_id, task_id=task_id)
                        full_text += chunk.delta
                        await session.emit("stream_delta", delta=chunk.delta, message_id=message_id, task_id=task_id)
                    if chunk.tool_call_delta:
                        idx = chunk.tool_call_delta.get("index", 0)
                        prev_name = tool_calls_buffer.get(idx, {}).get("function", {}).get("name", "")
                        self._accumulate_tool_delta(tool_calls_buffer, chunk.tool_call_delta)
                        tc = tool_calls_buffer[idx]
                        new_name = tc["function"]["name"]
                        if new_name and not prev_name and tc["id"]:
                            await session.emit(
                                "tool_call_preparing",
                                tool=new_name,
                                call_id=tc["id"],
                                task_id=task_id,
                            )
                            await session.emit(
                                "turn_status",
                                phase="running_tool",
                                tool=new_name,
                                call_id=tc["id"],
                                task_id=task_id,
                            )
                    if chunk.stop_reason:
                        stop_reason = chunk.stop_reason
                    if chunk.usage:
                        d_prompt, d_completion, prompt_tokens, completion_tokens = _consume_usage(
                            chunk.usage, prev_usage
                        )
                        session.add_usage(
                            prompt_tokens=d_prompt,
                            completion_tokens=d_completion,
                        )
                        prev_usage = (
                            max(prev_usage[0], prompt_tokens),
                            max(prev_usage[1], completion_tokens),
                        )
                        session.context_tokens = prompt_tokens + completion_tokens
                        session.context_window = provider.context_window
                        await session.emit(
                            "token_update",
                            used=session.tokens_used,
                            budget=session.token_budget,
                            context_tokens=session.context_tokens,
                            context_window=session.context_window,
                        )
            except Exception as api_err:
                # 与主循环相同的脱敏规则：该消息被抛入 _execute_and_finish，
                # 由其持久化到 agent_tasks 并发出 task_failed——否则下方的
                # 链式异常会把原始文本写进任何回溯日志。
                scrubbed_tb = _scrub_credentials(traceback.format_exc(), self._credentials)
                _log.error(
                    "task provider.stream() failed: task_id=%s provider=%s messages=%d pending_tool_call_ids=%s error=%s",
                    task_id,
                    provider.model_name,
                    len(messages),
                    pending_tool_ids,
                    scrubbed_tb,
                )
                if classify_api_error(api_err) is StopReason.CONTEXT_EXCEEDED:
                    # 与下方停止原因分支使用相同消息，使任务卡片无论由哪条
                    # 路径捕获溢出，读起来都一致。
                    raise RuntimeError("Context window exceeded during task execution") from api_err
                raise RuntimeError(
                    f"LLM API error: {_scrub_credentials(str(api_err), self._credentials)}\n"
                    f"  task_id={task_id}, model={provider.model_name}, "
                    f"messages={len(messages)}, error_type={type(api_err).__name__}"
                ) from api_err

            # 当流结束时既无签名也无后续内容，关闭仍开着的思考块。
            if thinking_open:
                thinking_open = False
                await session.emit("thinking_end", message_id=message_id, task_id=task_id)

            # 与主循环相同的截断守卫：在工具调用中途被切断的任务轮次，不能
            # 让不完整的 tool_call 在历史中成为孤儿（下一次迭代会因消息
            # 序列返回 400）。
            tool_calls = (
                [tc for tc in tool_calls_buffer.values()
                 if tc.get("id") and tc.get("function", {}).get("name")]
                if tool_calls_buffer else None
            ) or None
            messages.append(Message(
                role="assistant", content=full_text, tool_calls=tool_calls,
                thinking_blocks=thinking_blocks or None,
            ))

            # ─── 停止原因处理（Stop Reason Handling）──────────────────
            if stop_reason == StopReason.REFUSAL:
                raise RuntimeError(f"Model refused: {full_text[:200]}")

            if stop_reason == StopReason.CONTEXT_EXCEEDED:
                raise RuntimeError("Context window exceeded during task execution")

            if stop_reason == StopReason.CONTENT_FILTER:
                raise RuntimeError("Content filtered by provider")

            if stop_reason == StopReason.MAX_TOKENS and not tool_calls:
                await session.emit("warning", message="Task response truncated (max_tokens)")
                await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                return "".join([*full_text_total, full_text])

            if stop_reason == StopReason.PAUSE_TURN:
                pause_count += 1
                if pause_count > max_pause_continuations:
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                    return "".join([*full_text_total, full_text])
                if not tool_calls:
                    full_text_total.append(full_text)
                    continue

            if not tool_calls:
                await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                return "".join([*full_text_total, full_text])

            # ─── 处理工具调用（Process Tool Calls）────────────────────
            full_text_total.append(full_text)
            pause_count = 0
            progress = self._compute_task_progress(task_id)

            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                tool_id = tc.get("id")
                if not tool_name or not tool_id:
                    continue
                try:
                    args = self._parse_tool_arguments(tc)
                except (json.JSONDecodeError, TypeError) as _e:
                    # 当下一次工具循环迭代序列化这条助手消息时，保留一份合法
                    # 的供应方历史。
                    tc["function"]["arguments"] = "{}"
                    bad_result = {"error": f"Malformed tool arguments (invalid JSON): {_e}"}
                    await session.emit("tool_call_start", tool=tool_name, input={}, call_id=tool_id, task_id=task_id)
                    await session.emit("turn_status", phase="running_tool", tool=tool_name, call_id=tool_id, task_id=task_id)
                    await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(bad_result), call_id=tool_id, task_id=task_id)
                    messages.append(Message(role="tool", content=_dumps(bad_result), tool_call_id=tool_id, name=tool_name))
                    continue

                await session.emit(
                    "tool_call_start",
                    tool=tool_name,
                    input=args,
                    call_id=tool_id,
                    task_id=task_id,
                    **progress,
                )
                await session.emit(
                    "turn_status",
                    phase="running_tool",
                    tool=tool_name,
                    call_id=tool_id,
                    task_id=task_id,
                )

                # plan_task 是对话循环的指令。子任务内没有可交给调度器的任务
                # 列表，而 PlannerTool 只会确认（ACK），静默执行它毫无作用——
                # 模型随后会误以为自己规划了永远不会运行的工作。
                if tool_name == "plan_task":
                    result = {
                        "error": (
                            "plan_task is only available at the top level; "
                            "continue executing the current task directly"
                        )
                    }
                else:
                    tool = self._tools.get(tool_name)
                    if tool is None:
                        result = {"error": f"Unknown tool: {tool_name}"}
                    else:
                        try:
                            result = await tool.execute(args, task_tool_ctx)
                        except asyncio.CancelledError:
                            result = {"error": "Tool execution interrupted"}
                            await session.emit("tool_call_end", tool=tool_name, output=_clip_tool_output(result), call_id=tool_id, task_id=task_id, status="error")
                            raise
                        except Exception as e:
                            _log.warning("Tool %s failed (call_id=%s): %s", tool_name, tool_id, e, exc_info=True)
                            result = {"error": str(e)[:500]}

                await session.emit(
                    "tool_call_end",
                    tool=tool_name,
                    output=_clip_tool_output(result),
                    call_id=tool_id,
                    task_id=task_id,
                    **progress,
                )

                # 与对话循环相同的类型守卫：有些工具在这些键下报告数量
                # （repo_graph 的 too_many_files 摘要带有 files=<整数>），而
                # 在此处遍历整数——位于工具 try/except 之外——会以
                # "'int' object is not iterable" 让整个子任务失败（级联到
                # 依赖方）。
                images = result.get("images")
                if isinstance(images, list) and images:
                    await session.emit("image_output", message_id=message_id, images=images)

                out_files = result.get("files")
                if isinstance(out_files, list) and out_files:
                    _deliverable = [f for f in out_files if isinstance(f, dict) and f.get("url")]
                    if _deliverable:
                        await session.emit("file_output", message_id=message_id, files=_deliverable)

                if tool_name == "knowledge_rw" and self._project_context:
                    op = args.get("operation")
                    # 与对话循环相同的守卫：失败的写入不能把从未落盘的内容
                    # 放进上下文。
                    if op == "write" and not result.get("error"):
                        from .knowledge.loader import update_context_file, _is_log_role
                        slug = args.get("slug", "")
                        content = args.get("content", "")
                        if slug and content and not _is_log_role(content):
                            update_context_file(self._project_context, slug, content)
                            self._invalidate_static_cache()
                            self._mark_prompt_dirty()
                            await session.emit("knowledge_updated")
                    elif op in ("load", "unload", "refresh", "delete", "purge"):
                        # 与对话循环相同：仅靠脏标志会从陈旧的静态缓存重建
                        # ——丢弃缓存，使被删除/刷新的知识真正离开提示词。
                        self._invalidate_static_cache()
                        self._mark_prompt_dirty()

                if tool_name == "memory_rw":
                    op = args.get("operation")
                    if op in ("save", "update", "archive"):
                        self._invalidate_static_cache()
                        self._mark_prompt_dirty()

                messages.append(Message(
                    role="tool",
                    content=_dumps(result),
                    tool_call_id=tool_id,
                    name=tool_name,
                ))

        # 循环退出：要么确实耗尽了 max_iterations，要么上方的中止检查在运行
        # 中途 break。中止不是迭代限制，也不能谎称达到限制；两种情况下
        # stream_end 都会关闭前端流。
        if not session.is_aborted():
            await session.emit("warning", message=f"Task {task_id} reached maximum tool iterations - reply to continue")
        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
        return "".join(full_text_total)
