"""OpenAI 供应方（Azure OpenAI 也复用本模块）。"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
)
from ..compressor import MAX_OUTPUT_RESERVE

def _context_window(model: str) -> int:
    m = model.lower()
    if m.startswith("gpt-5"):
        return 1_000_000
    if any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")):
        return 200_000
    # GLM-5.3 是旗舰（100 万上下文）；更早的 GLM 系列走回退值。用版本元组比较，
    # 而非 float()："5.3.1" 根本不是浮点数（会抛错），而 float() 会让 5.10 排在
    # 5.3 之下。
    glm_ver = re.match(r"^glm[-_]?(\d+(?:\.\d+)*)", m)
    if glm_ver:
        version = tuple(int(part) for part in glm_ver.group(1).split("."))
        if version >= (5, 3):
            return 1_000_000
    if "gemini" in m:
        return 1_000_000
    # gpt-4o、gpt-4-turbo、gpt-4o-mini 等。
    return 128_000


def _http_timeout() -> httpx.Timeout:
    """供应方 HTTP 客户端的分阶段请求超时。

    对每个阶段套用 60 秒标量会使读取阶段也只有 60 秒：大提示词的流式（10 万
    token 以上历史）首个数据块通常需要一分多钟，因此轮次在 SDK 重试后以
    “Request timed out” 失败。openai SDK 会采用自定义 http_client 的超时作为
    逐请求超时（覆盖其自身 600 秒默认值），因此各阶段必须在这里设对：读取宽松、
    连接收紧、写入按多 MB 上传量设定。
    """
    return httpx.Timeout(300.0, connect=10.0, write=120.0, pool=60.0)


class OpenAIProvider(LLMProvider):
    """通过 openai SDK 调用 OpenAI。"""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str | None = None, ssl_verify: bool = False, url_mode: str = "base_url", context_window: int | None = None, thinking_enabled: bool | None = None, reasoning_effort: str | None = None) -> None:
        # 为 secret_values()/scrub() 保留——SDK 自己持有一份副本但不提供访问器，
        # 而异常文本必须可脱敏。
        self._api_key = api_key
        kwargs: dict = {"api_key": api_key}
        if base_url:
            b = base_url.rstrip("/")
            if url_mode == "full_url":
                # 用户提供了完整的端点 URL。
                # 若带 /chat/completions 后缀则剥离（SDK 会重新追加）。
                if b.endswith("/chat/completions"):
                    b = b[: -len("/chat/completions")]
                kwargs["base_url"] = b
            else:
                # 基础 URL 模式：未检测到版本段时自动追加 /v1。
                kwargs["base_url"] = b if re.search(r"/v\d+$", b) else f"{b}/v1"
        kwargs["http_client"] = httpx.AsyncClient(verify=ssl_verify, timeout=_http_timeout())
        # 保留一次重试仍能应对偶发的 429/5xx；SDK 默认值（2）会让一个已死端点
        # 在报错前拖到 3 次 300 秒读取。
        kwargs["max_retries"] = 1
        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        # 来自“设置 → AI 模型”的逐配置覆盖；None = 名称匹配默认值。
        self._context_window_override = context_window
        # OpenAI 的思维链从兼容网关以只解析方式到达——没有请求侧的思考开关，因此
        # thinking_enabled 接受但忽略。reasoning_effort 则是请求侧的：只发给推理
        # 模型（见 _reasoning_kwargs）。
        self._reasoning_effort = reasoning_effort

    async def close(self) -> None:
        """关闭 SDK 的 httpx 客户端（连接池）。

        Agent.close() 会遍历 _provider_cache 对每个供应方调用 close()；没有这个
        重写，每条消息都会泄漏一个保活连接池，伴随整个进程生命周期。
        """
        try:
            await self._client.close()
        except Exception:
            pass

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def context_window(self) -> int:
        return self._context_window_override or _context_window(self._model)

    @property
    def supports_tool_calls(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        _NO_VISION = ("gpt-3.5",)
        return not any(self._model.startswith(p) for p in _NO_VISION)

    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        out = []
        for msg in messages:
            if msg.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
                })
            elif isinstance(msg.content, list):
                content = []
                for part in msg.content:
                    if part.get("type") == "image":
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{part['media_type']};base64,{part['data']}"
                            },
                        })
                    else:
                        content.append({"type": "text", "text": part.get("text", "")})
                d: dict = {"role": msg.role, "content": content}
                if msg.tool_calls:
                    d["tool_calls"] = msg.tool_calls
                out.append(d)
            else:
                d: dict = {"role": msg.role, "content": msg.content}
                if msg.tool_calls:
                    d["tool_calls"] = msg.tool_calls
                out.append(d)
        return out

    def _to_openai_tools(self, tools: list[ToolDefinition]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _is_reasoning_model(self) -> bool:
        """o 系列 / gpt-5 拒绝 max_tokens 和 temperature（HTTP 400）。

        与 AzureOpenAIProvider 的重写对应；保留在基类上，使推理模型的普通
        “openai” 类型配置也能工作。
        """
        m = self._model.lower()
        return any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")) or "gpt-5" in m

    def _clamp_max_tokens(self, max_tokens: int) -> int:
        """把输出 token 限制在非推理模型的上限内。

        Agent 配置默认 max_tokens 为 128000；gpt-4o 类模型把输出上限设为 16384，
        并以 400 拒绝原始值。推理模型接受较大的 max_completion_tokens，原样保留。
        GLM 也豁免：其思考在服务端进行且无法关闭，因此 gpt-4o 的上限会让推理加
        内容没有预算，在任何输出之前就截断智能体轮次（该端点接受 128000——已对
        系统默认部署验证）。
        """
        if self._is_reasoning_model():
            return max_tokens
        if re.match(r"^glm[-_]?\d", self._model.lower()):
            return max_tokens
        return min(max_tokens, MAX_OUTPUT_RESERVE)

    def _reasoning_kwargs(self) -> dict:
        """只有推理模型可以接收的请求关键字参数。

        在推理分支之外，`reasoning_effort` 会在 gpt-4o 类模型和 OpenAI 兼容网关
        （vLLM/Ollama 模型名）上报 400——一律以 _is_reasoning_model() 为门槛。
        """
        if self._is_reasoning_model() and self._reasoning_effort:
            return {"reasoning_effort": self._reasoning_effort}
        return {}

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.update(self._reasoning_kwargs())
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = Message(
            role="assistant",
            content=choice.message.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (choice.message.tool_calls or [])
            ] or None,
        )
        raw_stop = choice.finish_reason
        # 某些 OpenAI 兼容网关（vLLM/LM Studio/Ollama）不返回 usage——需防范
        # AttributeError，否则该网关上的压缩会永远静默失败。
        usage = response.usage
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            } if usage is not None else {},
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_openai(raw_stop),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.update(self._reasoning_kwargs())
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice:
                    delta = choice.delta
                    # OpenAI 兼容的推理网关（DeepSeek/GLM/vLLM）把思维链放在
                    # 这里——只解析，从不主动请求。官方 OpenAI 不设置它（无妨）。
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield StreamChunk(thinking=reasoning)
                    text = delta.content or ""
                    finish = choice.finish_reason
                    normalized = StopReason.from_openai(finish) if finish else None

                    if delta.tool_calls:
                        if text:
                            yield StreamChunk(delta=text, tool_call_delta=None, finish_reason=None)
                        for tc in delta.tool_calls:
                            tc_delta = {
                                "index": tc.index,
                                "id": tc.id or "",
                                "function": {
                                    "name": getattr(tc.function, "name", "") or "",
                                    "arguments": getattr(tc.function, "arguments", "") or "",
                                },
                            }
                            yield StreamChunk(
                                delta="",
                                tool_call_delta=tc_delta,
                                finish_reason=finish,
                                stop_reason=normalized,
                            )
                    else:
                        yield StreamChunk(
                            delta=text,
                            tool_call_delta=None,
                            finish_reason=finish,
                            stop_reason=normalized,
                        )
                # 用量独立于 choices：LiteLLM 把最终用量对象挂在仍带空 choice 的
                # 数据块上，vLLM 在普通数据块上报告运行统计。以空 choices 列表为
                # 门槛会把这些计数全部丢弃。
                if chunk.usage:
                    yield StreamChunk(
                        usage={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        },
                    )
        finally:
            # 否则流式中途的任务取消会让 httpx 连接一直开到 Agent.close()；
            # 在这里关闭。
            await stream.close()

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return response.data[0].embedding


class AzureOpenAIProvider(OpenAIProvider):
    """Azure OpenAI——使用 AsyncAzureOpenAI SDK 以正确路由部署 URL。"""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "",
        model: str = "gpt-4o",
        api_version: str = "2024-08-01-preview",
        ssl_verify: bool = False,
        context_window: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        **_kwargs,
    ) -> None:
        from openai import AsyncAzureOpenAI
        base = endpoint.strip().rstrip("/")
        parsed = urlsplit(base)
        if not base:
            raise ValueError("Azure OpenAI requires an endpoint")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Azure OpenAI endpoint must be an HTTP or HTTPS URL with a hostname")
        # 与 OpenAIProvider 相同的 _api_key 契约（这里不调用 super().__init__）。
        self._api_key = api_key
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=base,
            azure_deployment=model,
            api_version=api_version,
            http_client=httpx.AsyncClient(verify=ssl_verify, timeout=_http_timeout()),
            max_retries=1,
        )
        self._model = model
        self._display_model = model
        # 来自“设置 → AI 模型”的逐配置覆盖；None = 名称匹配默认值。
        self._context_window_override = context_window
        # 显式参数（不被 **_kwargs 吞掉）：Azure 配置必须与普通 openai 配置一样
        # 采用相同的思考/努力覆盖——静默丢弃会让仅 Azure 的环境毫无报错。
        # thinking_enabled 本身仍被忽略：OpenAI 思维链只解析。
        self._reasoning_effort = reasoning_effort

    def _is_reasoning_model(self) -> bool:
        m = self._model.lower()
        return any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")) or "gpt-5" in m

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
        )
        # Azure 对非推理模型拒绝 max_completion_tokens（gpt-4o 和更早的 API 版本
        # 会报 400）。基类按推理模型分流——Azure 也必须如此。
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.update(self._reasoning_kwargs())
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = Message(
            role="assistant",
            content=choice.message.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (choice.message.tool_calls or [])
            ] or None,
        )
        raw_stop = choice.finish_reason
        # 某些 OpenAI 兼容网关（vLLM/LM Studio/Ollama）不返回 usage——需防范
        # AttributeError，否则该网关上的压缩会永远静默失败。
        usage = response.usage
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            } if usage is not None else {},
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_openai(raw_stop),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
            kwargs.update(self._reasoning_kwargs())
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice:
                    delta = choice.delta
                    # 与上面 OpenAI 路径相同的推理通道解析——Azure 前置的网关
                    # （经 Azure 的 DeepSeek）也会发出它。
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield StreamChunk(thinking=reasoning)
                    text = delta.content or ""
                    finish = choice.finish_reason
                    normalized = StopReason.from_openai(finish) if finish else None

                    if delta.tool_calls:
                        if text:
                            yield StreamChunk(delta=text, tool_call_delta=None, finish_reason=None)
                        for tc in delta.tool_calls:
                            tc_delta = {
                                "index": tc.index,
                                "id": tc.id or "",
                                "function": {
                                    "name": getattr(tc.function, "name", "") or "",
                                    "arguments": getattr(tc.function, "arguments", "") or "",
                                },
                            }
                            yield StreamChunk(
                                delta="",
                                tool_call_delta=tc_delta,
                                finish_reason=finish,
                                stop_reason=normalized,
                            )
                    else:
                        yield StreamChunk(
                            delta=text,
                            tool_call_delta=None,
                            finish_reason=finish,
                            stop_reason=normalized,
                        )
                # 与 OpenAI 路径相同的独立用量捕获：网关可能把用量挂在仍带 choice
                # 的数据块上。
                if chunk.usage:
                    yield StreamChunk(
                        usage={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        },
                    )
        finally:
            # 否则流式中途的任务取消会让 httpx 连接一直开到 Agent.close()；
            # 在这里关闭。
            await stream.close()
