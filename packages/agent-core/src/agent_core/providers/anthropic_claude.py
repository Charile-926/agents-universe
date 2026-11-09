"""Anthropic Claude 供应方。"""
from __future__ import annotations

import json
from typing import AsyncIterator

import anthropic
import httpx

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
    thinking_enabled_by_env,
)

def _context_window(model: str) -> int:
    model = model.lower()
    if any(name in model for name in (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    )):
        return 1_000_000
    # 其余模型（haiku-4-5 及任何旧版/未知模型）的默认值。
    return 200_000


# Anthropic Messages API 逐模型的输出上限（max_tokens）。Agent 配置默认 max_tokens
# 为 128000（agent.py 的 AgentConfig），每个 Anthropic 模型都以 HTTP 400 拒绝它——
# API 不接受高于模型实际输出上限的天花板。像 OpenAI 供应方的 _clamp_max_tokens
# 一样限制，但按模型区分：haiku 类上限 8k，sonnet 类 64k，opus 类 128k。未识别
# 模型采用 sonnet 的 64k——这是仍能容纳所有已知 Anthropic 家族的最安全上限
# （haiku 的 8k 对未来模型会造成不必要的限制，而 64k 不超过任何当前上限）。
def _max_output_tokens(model: str) -> int:
    m = model.lower()
    if "haiku" in m:
        return 8_192
    if "opus" in m:
        return 128_000
    # sonnet 类及任何未识别模型。
    return 64_000


# 当代 API 接受自适应思考参数的家族。4.6+/5 家族会以 HTTP 400 拒绝
# budget_tokens——绝不能给它们发该参数。与 _context_window 的模型列表保持同步。
_ADAPTIVE_THINKING_FAMILIES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def _thinking_rejected(exc: BaseException) -> bool:
    """当请求因 `thinking` 字段本身而失败时为真。

    用于一次性的“去掉思考后重试”回退：企业网关（Bedrock 风格代理）可能直接拒绝
    该参数。只有错误文本提到 "thinking" 才算——无关的 400 绝不能静默关闭思考。
    """
    text = str(exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            text += " " + resp.text
        except Exception:
            pass
    return "thinking" in text.lower()


def _content_blocks(content: str | list) -> list[dict]:
    """把一个条目的内容字段规范化为块列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content)


def _merge_same_role_turns(out: list[dict]) -> list[dict]:
    """把相邻的同角色轮次折叠到一起。

    直连 Messages API 会合并连续的同角色轮次，但 Bedrock 风格网关会直接拒绝请求
    （"roles must alternate between user and assistant"）。Agent 历史确实会产生
    这种轮次：在一个步骤边界出队的多个注入、紧接工具结果轮次之后被消费的注入、
    下一轮被重放到其父轮次之前的委派子智能体持久化回复，以及追加在暂停的助手轮次
    之后的 pause_turn 续接。工具结果已经位于其用户轮次开头，因此追加的文本按 API
    预期落在它们之后。
    """
    merged: list[dict] = []
    for entry in out:
        if not merged or merged[-1]["role"] != entry["role"]:
            merged.append(entry)
            continue
        prev = merged[-1]
        blocks = _content_blocks(entry["content"])
        if entry["role"] == "assistant":
            # 带签名的思考块必须位于助手轮次内容的开头。
            thinking = [b for b in blocks if b.get("type") == "thinking"]
            other = [b for b in blocks if b.get("type") != "thinking"]
            prev["content"] = thinking + _content_blocks(prev["content"]) + other
        else:
            prev["content"] = _content_blocks(prev["content"]) + blocks
    return merged


class AnthropicClaudeProvider(LLMProvider):
    """通过 anthropic SDK（直连）或原生 httpx（网关）调用 Anthropic Claude。

    直连 Anthropic API：传入标准 api_key（sk-ant-...）。
    对接 Bedrock 兼容的企业网关：把 api_key 作为 Bearer 令牌传入，base_url 指向
    网关——使用原生 httpx（不依赖 botocore）。
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", base_url: str | None = None, ssl_verify: bool = False, url_mode: str = "base_url", context_window: int | None = None, thinking_enabled: bool | None = None, reasoning_effort: str | None = None) -> None:
        self._model = model
        self._url_mode = url_mode
        # 扩展思考开关：显式的逐配置覆盖优先，否则取 AGENT_EXTENDED_THINKING
        # （默认开启）。当网关以与思考相关的 400 拒绝该参数时，也会在本实例
        # 生命周期内关闭它。reasoning_effort 仅 OpenAI 使用——这里接受它，使
        # 与供应方无关的凭证字典绝不会让某轮 TypeError。
        self._thinking_enabled = thinking_enabled if thinking_enabled is not None else thinking_enabled_by_env()
        # 来自“设置 → AI 模型”的逐配置覆盖；None = 名称匹配默认值。
        self._context_window_override = context_window
        # 用精确主机比较，而非子串：域名仅仅“包含” api.anthropic.com 的网关
        # （如 api.anthropic.com.evil.example）不能被误判为官方 API，从而被迫走
        # 超时/头行为不同的 AsyncAnthropic SDK 路径。
        from urllib.parse import urlparse
        _host = urlparse(base_url).hostname if base_url else None
        self._is_gateway = bool(_host and _host.lower() != "api.anthropic.com")
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")

        if self._is_gateway:
            # 分阶段超时：读取必须撑过大提示词的首 token 等待；普通 120 秒标量
            # 还会限制写入密集的请求体。
            self._http = httpx.AsyncClient(
                verify=ssl_verify,
                timeout=httpx.Timeout(300.0, connect=10.0, write=120.0, pool=60.0),
            )
        else:
            import os
            # Anthropic SDK 在 1.0 把 HTTP 传输从 httpx 换成了 httpx2：给 1.x
            # AsyncAnthropic 传入 httpx.AsyncClient 会抛
            # TypeError("Expected an instance of httpx2.AsyncClient ...")。
            # 选择与已安装 SDK 匹配的传输，使当前 SDK（CI 安装 anthropic 1.2）
            # 和较旧的 0.x 都能继续工作。
            try:
                import httpx2
            except ImportError:  # mcp2 之前的环境没有 httpx2 包
                httpx2 = None
            try:
                _pairs = (getattr(anthropic, "__version__", "0") or "0").split(".")[:2]
                _uses_httpx2 = tuple(int(p) for p in _pairs) >= (1, 0)
            except (ValueError, TypeError):
                _uses_httpx2 = httpx2 is not None
            _http_lib = httpx2 if _uses_httpx2 and httpx2 is not None else httpx
            http_client = _http_lib.AsyncClient(
                verify=ssl_verify,
                timeout=_http_lib.Timeout(300.0, connect=10.0, write=120.0, pool=60.0),
            )
            # 保留一个引用，使即使 SDK 初始化失败，close() 也能释放它。
            self._http = http_client
            kwargs: dict = {"api_key": api_key, "http_client": http_client, "max_retries": 1}
            if base_url:
                kwargs["base_url"] = base_url
            _proxy_keys = ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"]
            _saved = {k: os.environ.pop(k) for k in _proxy_keys if k in os.environ}
            try:
                self._client = anthropic.AsyncAnthropic(**kwargs)
            except Exception:
                # __init__ 里无法 await——若有事件循环在运行，则调度关闭。
                import asyncio
                try:
                    asyncio.get_running_loop().create_task(http_client.aclose())
                except RuntimeError:
                    pass
                raise
            finally:
                os.environ.update(_saved)

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
        return True

    def _thinking_param(self, max_tokens: int) -> dict | None:
        """本模型请求体的 `thinking` 字段，关闭时为 None。

        - 4.6+/5 家族采用自适应形态。`display: "summarized"` 是关键：Fable/
          Mythos/Opus 5/Sonnet 5 上默认 display 为 "omitted"，会流式输出空的
          思考增量——没有它，整个思考界面会静默毫无反应。
        - haiku-4-5 及更旧模型采用旧版 budget_tokens 形态（>=1024 且严格小于
          max_tokens；绝不发给 4.6+/5——它们会对此报 400）。
        - 未知的 Claude 名称默认采用自适应，这是对 budget 400 最安全的选择；
          早于自适应的网关由“去掉思考后重试”回退覆盖。
        """
        if not self._thinking_enabled:
            return None
        m = self._model.lower()
        if any(name in m for name in _ADAPTIVE_THINKING_FAMILIES):
            return {"type": "adaptive", "display": "summarized"}
        if "haiku" in m:
            budget = min(4096, max_tokens - 1024)
            if budget < 1024:
                # max_tokens 太小，放不下合法预算——跳过思考。
                return None
            return {"type": "enabled", "budget_tokens": budget}
        return {"type": "adaptive", "display": "summarized"}

    def _disable_thinking(self, reason: str) -> None:
        """在本供应方实例的剩余生命周期内关闭思考。"""
        if self._thinking_enabled:
            self._thinking_enabled = False
            import logging
            logging.getLogger(__name__).warning(
                "Extended thinking disabled for %s: %s", self._model, reason,
            )

    def _to_anthropic_messages(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        """转换为 Anthropic 格式。返回（系统提示词, 消息列表）。"""
        system = None
        out: list[dict] = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content if isinstance(msg.content, str) else str(msg.content)
            elif msg.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
                }
                # 并行工具调用会产生连续的工具消息。每个都不能变成独立的用户轮次
                # ——API 拒绝连续的用户消息（"roles must alternate"）。当前一条用户
                # 消息只含 tool_result 部分时，合并进它。
                if (
                    out
                    and out[-1]["role"] == "user"
                    and isinstance(out[-1]["content"], list)
                    and all(p.get("type") == "tool_result" for p in out[-1]["content"])
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif msg.role == "assistant" and msg.tool_calls:
                content = []
                # 当助手轮次携带 tool_use 且思考开启时，带签名的思考块必须先回放。
                # 只有内存中的（同轮）块能到达这里——签名从不持久化，因此压缩历史
                # 无法携带会导致 400 的过期块。
                for blk in msg.thinking_blocks or []:
                    if blk.get("signature"):
                        content.append(blk)
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    # 被截断的工具调用（流式在 max_tokens 处中断）可能携带非法的
                    # 参数 JSON；这里抛错会中止整个回放。镜像 agent.py 的守卫：
                    # 退化为 {}，使 API 调用仍能发出。
                    try:
                        input_args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        input_args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": input_args,
                    })
                out.append({"role": "assistant", "content": content})
            elif isinstance(msg.content, list):
                content = []
                for part in msg.content:
                    if part.get("type") == "image":
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": part["media_type"],
                                "data": part["data"],
                            },
                        })
                    else:
                        content.append({"type": "text", "text": part.get("text", "")})
                out.append({"role": msg.role, "content": content})
            else:
                # 带内存思考块的普通助手文本：API 要求块列表（思考在前，文本在后）
                # ——裸字符串会丢弃这些块。
                blocks = [b for b in (msg.thinking_blocks or []) if b.get("signature")]
                if msg.role == "assistant" and blocks:
                    content: list[dict] = list(blocks)
                    if msg.content:
                        content.append({"type": "text", "text": msg.content})
                    out.append({"role": msg.role, "content": content})
                else:
                    out.append({"role": msg.role, "content": msg.content})
        return system, _merge_same_role_turns(out)

    def _to_anthropic_tools(self, tools: list[ToolDefinition]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def _parse_response(self, response) -> CompletionResult:
        text_parts = []
        tool_calls = []
        thinking_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                # 无签名块仅供展示，绝不能回放到 API。
                sig = getattr(block, "signature", None)
                if sig:
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                        "signature": sig,
                    })
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
        msg = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            thinking_blocks=thinking_blocks or None,
        )
        raw_stop = response.stop_reason or "stop"
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
            },
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_anthropic(raw_stop),
        )

    def _parse_raw_response(self, data: dict) -> CompletionResult:
        """解析网关返回的原始 JSON 响应（与 Anthropic Messages API 同 schema）。"""
        text_parts = []
        tool_calls = []
        thinking_blocks = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                sig = block.get("signature")
                if sig:
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": sig,
                    })
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        msg = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            thinking_blocks=thinking_blocks or None,
        )
        usage = data.get("usage", {})
        raw_stop = data.get("stop_reason", "stop")
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
            model=data.get("model", self._model),
            finish_reason=raw_stop,
            stop_reason=StopReason.from_anthropic(raw_stop),
        )

    # ─── 网关（原生 httpx，不依赖 botocore）──────────────────────────────

    def _gateway_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _gateway_payload(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> dict:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        clamped_max = min(max_tokens, _max_output_tokens(self._model))
        payload: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": clamped_max,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._to_anthropic_tools(tools)
        thinking_param = self._thinking_param(clamped_max) if thinking else None
        if thinking_param:
            payload["thinking"] = thinking_param
        else:
            # 思考开启时，Messages API 拒绝除 1 以外的任何 temperature。省略该字段
            # 保留 API 默认值（1）；发送 Agent 的 0.0 会让每次调用都报 400，触发
            # “去掉思考后重试”回退，从而静默关闭思考。
            payload["temperature"] = temperature
        return payload

    async def _gateway_complete(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> CompletionResult:
        if self._url_mode == "full_url":
            url = self._base_url
        else:
            url = f"{self._base_url}/model/{self._model}/invoke"
        payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
        try:
            resp = await self._http.post(url, headers=self._gateway_headers(), json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 网关拒绝了思考字段——去掉它重试一次。只有 400 才可能表示“参数
            # 非法”；对仅提到 thinking 的 429/5xx 响应体重试会加倍压力，并在本轮
            # 剩余时间静默关闭思考。
            if (
                thinking and "thinking" in payload
                and exc.response.status_code == 400
                and _thinking_rejected(exc)
            ):
                self._disable_thinking(f"gateway rejected thinking param: {exc}")
                return await self._gateway_complete(
                    messages, tools, max_tokens, temperature, thinking=False,
                )
            raise
        return self._parse_raw_response(resp.json())

    @staticmethod
    def _completion_to_chunks(result: CompletionResult) -> list[StreamChunk]:
        """非流式结果经流式接口回放时所用的数据块。

        思考文本和签名先发出，使 Agent 循环既能展示它们，又能为下一次工具循环
        请求保留带签名的块。
        """
        chunks: list[StreamChunk] = []
        for blk in result.message.thinking_blocks or []:
            if blk.get("thinking"):
                chunks.append(StreamChunk(thinking=blk["thinking"]))
            if blk.get("signature"):
                chunks.append(StreamChunk(thinking_signature=blk["signature"]))
        chunks.append(StreamChunk(delta=result.message.content))
        if result.message.tool_calls:
            for i, tc in enumerate(result.message.tool_calls):
                chunks.append(StreamChunk(tool_call_delta={
                    "index": i, "id": tc["id"],
                    "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                }))
        # stop_reason 必须随附：Agent 循环以规范化字段为判断依据，因此在这里丢弃
        # 它，会让拒绝/上下文超限/达到 max_tokens 的结尾在非流式网关回退中退化为
        # 静默的普通补全。
        chunks.append(StreamChunk(
            finish_reason=result.finish_reason,
            stop_reason=result.stop_reason,
            usage=result.usage,
        ))
        return chunks

    async def _gateway_stream(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """从网关流式获取。先尝试 SSE 流式，失败则退化为非流式。"""
        if self._url_mode == "full_url":
            url = self._base_url
            payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
            payload["stream"] = True
        else:
            url = f"{self._base_url}/model/{self._model}/invoke-with-response-stream"
            payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
        headers = self._gateway_headers()

        try:
            async with self._http.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 404:
                    # 网关不支持流式端点——退化为非流式。
                    result = await self._gateway_complete(messages, tools, max_tokens, temperature, thinking=thinking)
                    for chunk in self._completion_to_chunks(result):
                        yield chunk
                    return

                if resp.status_code >= 400:
                    # 先读取响应体，使思考拒绝检查能检查它；仅 raise_for_status
                    # 会丢失文本。
                    await resp.aread()
                    if (
                        thinking and "thinking" in payload
                        and resp.status_code == 400
                        and _thinking_rejected(httpx.HTTPStatusError(
                            "thinking rejected", request=resp.request, response=resp,
                        ))
                    ):
                        self._disable_thinking(f"gateway rejected thinking param: HTTP {resp.status_code}")
                        async for chunk in self._gateway_stream(
                            messages, tools, max_tokens, temperature, thinking=False,
                        ):
                            yield chunk
                        return
                    resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    async for chunk in self._parse_sse(resp):
                        yield chunk
                else:
                    # 不是 SSE——读取完整响应体并按 JSON 解析。
                    body = b""
                    async for raw in resp.aiter_bytes():
                        body += raw
                    data = json.loads(body)
                    result = self._parse_raw_response(data)
                    for chunk in self._completion_to_chunks(result):
                        yield chunk

        except httpx.HTTPStatusError as exc:
            # 只在网关拒绝流式端点（缺失或不支持）时回退。在 429/401/400 时
            # 以非流式重新发送相同载荷会加倍限流压力，并用回退的失败替换原始状态。
            if exc.response.status_code not in (404, 405, 501):
                raise
            result = await self._gateway_complete(messages, tools, max_tokens, temperature, thinking=thinking)
            for chunk in self._completion_to_chunks(result):
                yield chunk

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[StreamChunk]:
        """解析网关流式响应中的服务器发送事件（SSE）。"""
        input_tokens = 0
        tool_call_ids: dict[int, str] = {}

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            # SSE 规范允许 "data: <payload>" 和 "data:<payload>" 两种形式；某些
            # 网关/nginx 配置会发出后者——只匹配带空格的形式会静默吞掉整个流
            # （空回复，无错误）。
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    event = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    continue

                event_type = event.get("type", "")

                if event_type == "message_start":
                    msg = event.get("message", {})
                    usage = msg.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    idx = event.get("index", 0)
                    if block.get("type") == "tool_use":
                        block_id = block.get("id", "")
                        tool_call_ids[idx] = block_id
                        yield StreamChunk(tool_call_delta={
                            "index": idx,
                            "id": block_id,
                            "function": {"name": block.get("name", ""), "arguments": ""},
                        })

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    idx = event.get("index", 0)
                    if delta.get("type") == "text_delta":
                        yield StreamChunk(delta=delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        yield StreamChunk(thinking=delta.get("thinking", ""))
                    elif delta.get("type") == "signature_delta":
                        yield StreamChunk(thinking_signature=delta.get("signature", ""))
                    elif delta.get("type") == "input_json_delta":
                        yield StreamChunk(tool_call_delta={
                            "index": idx,
                            "id": tool_call_ids.get(idx, ""),
                            "function": {"name": "", "arguments": delta.get("partial_json", "")},
                        })

                elif event_type == "message_delta":
                    delta = event.get("delta", {})
                    usage = event.get("usage", {})
                    raw_stop = delta.get("stop_reason")
                    yield StreamChunk(
                        finish_reason=raw_stop,
                        stop_reason=StopReason.from_anthropic(raw_stop) if raw_stop else None,
                        usage={
                            "prompt_tokens": input_tokens,
                            "completion_tokens": usage.get("output_tokens", 0),
                        } if usage else None,
                    )

                elif event_type == "error":
                    # 直连 SDK 会对流式中途的错误事件抛出；网关路径也必须如此。
                    # 在这里静默返回会把本轮以截断回复的普通 END_TURN 结束。
                    err = event.get("error") or {}
                    raise RuntimeError(
                        "Anthropic gateway stream error: "
                        f"{err.get('type') or 'error'}: {err.get('message') or 'no message'}"
                    )

    # ─── 生命周期 ────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._is_gateway:
            await self._http.aclose()
        else:
            await self._client.close()

    # ─── 公开 API ───────────────────────────────────────────────────────

    def _request_kwargs(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> dict:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        clamped_max = min(max_tokens, _max_output_tokens(self._model))
        kwargs: dict = dict(
            model=self._model,
            max_tokens=clamped_max,
            messages=anthropic_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)
        thinking_param = self._thinking_param(clamped_max) if thinking else None
        if thinking_param:
            kwargs["thinking"] = thinking_param
        else:
            # 思考开启时，除 1 以外的任何 temperature 都会被拒绝；API 默认值是 1，
            # 因此这种情况下省略该字段。
            kwargs["temperature"] = temperature
        return kwargs

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        if self._is_gateway:
            return await self._gateway_complete(messages, tools, max_tokens, temperature)

        kwargs = self._request_kwargs(messages, tools, max_tokens, temperature)
        try:
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:
            # 网关/代理拒绝了思考字段——去掉它重试一次。
            if "thinking" in kwargs and _thinking_rejected(exc):
                self._disable_thinking(f"request rejected thinking param: {exc}")
                kwargs.pop("thinking", None)
                # 重试去掉了思考参数，因此为它省略的 temperature 可以加回来。
                kwargs.setdefault("temperature", temperature)
                response = await self._client.messages.create(**kwargs)
            else:
                raise
        return self._parse_response(response)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        if self._is_gateway:
            async for chunk in self._gateway_stream(messages, tools, max_tokens, temperature):
                yield chunk
            return

        kwargs = self._request_kwargs(messages, tools, max_tokens, temperature)
        emitted = False
        while True:
            try:
                async with self._client.messages.stream(**kwargs) as stream_ctx:
                    input_tokens = 0
                    tool_call_ids: dict[int, str] = {}

                    async for event in stream_ctx:
                        if hasattr(event, "type"):
                            if event.type == "message_start":
                                msg_usage = getattr(getattr(event, "message", None), "usage", None)
                                if msg_usage:
                                    input_tokens = getattr(msg_usage, "input_tokens", 0)
                            elif event.type == "content_block_start":
                                block = getattr(event, "content_block", None)
                                if block and getattr(block, "type", None) == "tool_use":
                                    block_id = getattr(block, "id", None) or ""
                                    tool_call_ids[event.index] = block_id
                                    yield StreamChunk(tool_call_delta={
                                        "index": event.index,
                                        "id": block_id,
                                        "function": {
                                            "name": block.name,
                                            "arguments": "",
                                        },
                                    })
                                    emitted = True
                            elif event.type == "content_block_delta":
                                delta = event.delta
                                if getattr(delta, "type", None) == "thinking" or hasattr(delta, "thinking"):
                                    # ThinkingDelta 有 .thinking 而非 .text——在
                                    # 文本分支会静默丢弃它之前先检查。
                                    yield StreamChunk(thinking=delta.thinking)
                                    emitted = True
                                elif getattr(delta, "type", None) == "signature" or hasattr(delta, "signature"):
                                    yield StreamChunk(thinking_signature=delta.signature)
                                    emitted = True
                                elif hasattr(delta, "text"):
                                    yield StreamChunk(delta=delta.text)
                                    emitted = True
                                elif hasattr(delta, "partial_json"):
                                    yield StreamChunk(tool_call_delta={
                                        "index": event.index,
                                        "id": tool_call_ids.get(event.index, ""),
                                        "function": {
                                            "name": "",
                                            "arguments": delta.partial_json,
                                        },
                                    })
                                    emitted = True
                            elif event.type == "message_delta":
                                usage = getattr(event, "usage", None)
                                raw_stop = event.delta.stop_reason
                                yield StreamChunk(
                                    finish_reason=raw_stop,
                                    stop_reason=StopReason.from_anthropic(raw_stop) if raw_stop else None,
                                    usage={
                                        "prompt_tokens": input_tokens,
                                        "completion_tokens": usage.output_tokens if usage else 0,
                                    } if usage else None,
                                )
                                emitted = True
                return
            except Exception as exc:
                # 400 在流式上下文打开时（任何事件之前）浮现——一旦数据块已发出，
                # 就不能重启流，否则客户端会看到重复内容。
                if not emitted and "thinking" in kwargs and _thinking_rejected(exc):
                    self._disable_thinking(f"stream rejected thinking param: {exc}")
                    kwargs.pop("thinking", None)
                    # 重试去掉了思考参数，因此为它省略的 temperature 可以加回来
                    # ——与 complete() 回退对应；否则重试请求会以 API 默认 1.0
                    # 而非配置值运行。
                    kwargs.setdefault("temperature", temperature)
                    continue
                raise
