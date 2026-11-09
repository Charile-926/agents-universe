"""LLM 供应方抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator


class StopReason(str, Enum):
    """所有供应方统一规范化后的停止原因。

    Anthropic：end_turn、tool_use、max_tokens、pause_turn、refusal、
    stop_sequence、model_context_window_exceeded
    OpenAI：stop、tool_calls、length、content_filter
    """
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    CONTEXT_EXCEEDED = "context_exceeded"
    UNKNOWN = "unknown"

    @classmethod
    def from_anthropic(cls, raw: str | None) -> "StopReason":
        if not raw:
            return cls.UNKNOWN
        mapping = {
            "end_turn": cls.END_TURN,
            "stop_sequence": cls.END_TURN,
            "tool_use": cls.TOOL_USE,
            "max_tokens": cls.MAX_TOKENS,
            "pause_turn": cls.PAUSE_TURN,
            "refusal": cls.REFUSAL,
            # 当请求超出模型上下文窗口时 Anthropic 的停止原因。未映射时会变成
            # UNKNOWN，Agent 把它当作普通 end_turn——context_exceeded 事件（以及
            # 界面的“压缩后重试”功能）永远不会触发。
            "model_context_window_exceeded": cls.CONTEXT_EXCEEDED,
        }
        return mapping.get(raw, cls.UNKNOWN)

    @classmethod
    def from_openai(cls, raw: str | None) -> "StopReason":
        if not raw:
            return cls.UNKNOWN
        mapping = {
            "stop": cls.END_TURN,
            "tool_calls": cls.TOOL_USE,
            "length": cls.MAX_TOKENS,
            "content_filter": cls.CONTENT_FILTER,
        }
        return mapping.get(raw, cls.UNKNOWN)


# 在主流供应方 SDK 和常见 OpenAI 兼容网关中识别上下文窗口溢出的子串。仅当 HTTP
# 状态码为 400 时才与小写化的异常文本匹配——不含这些标记的 400 仍为 UNKNOWN，
# 使模式/鉴权错误保持走 api_error 路径。
_CONTEXT_OVERFLOW_MARKERS = (
    # OpenAI SDK / Azure OpenAI / OpenAI 兼容网关
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    # Anthropic SDK（作为 BadRequestError 抛出，区别于流式的
    # model_context_window_exceeded 停止原因）
    "prompt is too long",
    "model_context_window_exceeded",
    # 谷歌 Gemini SDK
    "input token count exceeds",
    "reduce the length of the input",
    "input length and",
    # 通用网关措辞（"context window exceeded"、"too long for the model's
    # context window"）
    "too long for",
)


def classify_api_error(exc: BaseException) -> StopReason:
    """在上下文窗口溢出时把供应方/SDK 异常映射为 CONTEXT_EXCEEDED。

    刻意采用鸭子类型：SDK（openai/anthropic/google-genai）是只经注册表加载的
    可选依赖，因此这里不能导入它们的异常类。状态码从抛出层携带的任意属性读取；
    文本扫描覆盖不同的错误体形态（str(exc)、.message、.body、.details、HTTP
    响应）。

    分类只负责选择分支——调用方从不把异常文本转发给界面，因此这里无需凭证脱敏。
    """
    status = getattr(exc, "status_code", None)  # openai/anthropic APIStatusError
    if not isinstance(status, int):
        status = getattr(exc, "code", None)  # google-genai APIError.code
    if not isinstance(status, int):
        response = getattr(exc, "response", None)  # httpx.HTTPStatusError
        status = getattr(response, "status_code", None)
    if status == 413:
        # 聊天请求的负载过大 ≈ 溢出——无需文本匹配（网关对此的措辞千奇百怪）。
        return StopReason.CONTEXT_EXCEEDED
    if status != 400:
        return StopReason.UNKNOWN

    parts = [str(exc)]
    for attr in ("message", "body", "details"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    response = getattr(exc, "response", None)
    body_text = getattr(response, "text", None)
    if body_text:
        parts.append(str(body_text))
    text = " ".join(parts).lower()

    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return StopReason.CONTEXT_EXCEEDED
    # 单独的 "context window" 太宽泛（配置错误也会提到它）——要求它旁边出现溢出
    # 动词。
    if "context window" in text and any(
        verb in text for verb in ("exceed", "exceeded", "overflow", "too large")
    ):
        return StopReason.CONTEXT_EXCEEDED
    return StopReason.UNKNOWN


def thinking_enabled_by_env() -> bool:
    """共享的扩展思考开关（AGENT_EXTENDED_THINKING，默认开启）。

    必须主动请求思考的供应方（Anthropic、Gemini）会查询它；只解析已有推理通道
    的供应方则忽略它。
    """
    import os
    return os.environ.get("AGENT_EXTENDED_THINKING", "1").strip().lower() not in ("0", "false", "no")


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list  # 文本用 str；多模态（视觉）用 list
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # 用于 tool 角色消息
    # Anthropic 扩展思考块，仅用于同轮工具循环回放（当助手轮携带 tool_use 时，
    # API 要求在下一次请求中回传带签名的思考块）。绝不持久化到数据库——历史
    # 压缩会使签名失效。
    thinking_blocks: list[dict] | None = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema 对象


@dataclass
class StreamChunk:
    delta: str = ""
    tool_call_delta: dict | None = None
    finish_reason: str | None = None  # 供应方原始字符串
    stop_reason: StopReason | None = None  # 规范化后的值
    usage: dict | None = None  # {"prompt_tokens": int, "completion_tokens": int}
    # 扩展思考/推理输出（实践中与 delta 互斥：供应方每个事件要么产出可见文本，
    # 要么产出思考）。
    thinking: str = ""
    # 闭合思考块的 Anthropic 签名——在下一次工具循环请求中回放该块时必需。
    thinking_signature: str | None = None


@dataclass
class CompletionResult:
    message: Message
    usage: dict  # {"prompt_tokens": int, "completion_tokens": int}
    model: str
    finish_reason: str  # 供应方原始字符串
    stop_reason: StopReason = StopReason.UNKNOWN


class LLMProvider(ABC):
    """所有 LLM 供应方的抽象基类。"""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        """非流式补全。"""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        """流式补全，逐个产出 StreamChunk 对象。"""
        ...

    async def embed(self, text: str) -> list[float]:
        """为文本生成嵌入向量。可选能力——框架不使用。"""
        raise NotImplementedError(f"{type(self).__name__} does not support embeddings")

    @property
    @abstractmethod
    def context_window(self) -> int:
        """本供应方/模型支持的最大上下文 token 数。"""
        ...

    @property
    @abstractmethod
    def supports_tool_calls(self) -> bool:
        """本供应方是否支持工具/函数调用。"""
        ...

    @property
    @abstractmethod
    def supports_vision(self) -> bool:
        """本供应方是否支持图像输入。"""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """模型标识符字符串。"""
        ...

    async def close(self) -> None:
        """释放持有的资源（HTTP 客户端、连接）。可安全重复调用。"""

    def secret_values(self) -> list[str]:
        """本供应方实际发送到网络上的凭证值。

        每个具体供应方都把密钥存为 ``_api_key``。供应方异常（SDK 鉴权错误、
        h11 的头拒绝）可能内嵌密钥，因此记录异常文本的调用方要先经
        :meth:`scrub` 处理——token 绝不入日志（CLAUDE.md 的 token 安全规则）。
        """
        key = getattr(self, "_api_key", None)
        return [key] if isinstance(key, str) and key else []

    def scrub(self, text: str) -> str:
        """把 *text* 中 :meth:`secret_values` 的每个条目替换为 ``[REDACTED]``。

        同时匹配原始值和 h11/回溯所引用的字节 repr 转义形式——参见
        :func:`agent_core.tools._http.redact_secret`。
        """
        from ..tools._http import redact_secret
        for value in self.secret_values():
            text = redact_secret(text, value)
        return text
