"""基于 google-genai SDK 的 Google Gemini 供应方。

旧的 google.generativeai 包已停止维护（2024 年起无修复）；本模块面向其继任者
google-genai。新 SDK 的 Client 是实例级的，因此旧的 genai.configure() 进程全局
密钥（以及把 configure 和请求串行化的请求锁）已彻底移除。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import AsyncIterator

from google import genai
from google.genai import types as genai_types

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
    thinking_enabled_by_env,
)

# 在构造 Client 前后临时清空：google-genai 会在那里构建 httpx 客户端，否则会读取
# 环境中的代理变量（旧 genai.configure 路径强制的行为相同）。
_PROXY_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
]


def _continuation_target_index(
    seen_arg_keys: dict[int, set[str]], chunk_args: dict, fallback: int
) -> int:
    """选择一个无名续接块所延续的是哪一次工具调用。

    Gemini 会把较大的参数拆到多个数据块中；续接部分携带空名字，因此当并行函数
    调用交错时，“上一次调用”不一定是被延续的那个。按参数键与各调用已累积参数的
    重叠度匹配；到处都没有重叠时，退化为最近一次调用（旧行为）。
    """
    chunk_keys = set(chunk_args)
    target = fallback
    best = 0
    for idx, keys in seen_arg_keys.items():
        overlap = len(keys & chunk_keys)
        if overlap > best:
            best = overlap
            target = idx
    return target


def _context_window(model: str) -> int:
    m = model.lower()
    if "ultra" in m:
        return 32_768
    # 所有 Gemini 1.5+ 和 2.x+ 模型都有 100 万上下文。
    return 1_048_576


# 逐模型家族的输出 token 上限。Agent 配置默认 max_tokens 为 128000（agent.py 的
# AgentConfig），Gemini 会以 400 INVALID_ARGUMENT 拒绝它。Anthropic 和 OpenAI
# 供应方出于同样原因做限制；Gemini 不能原样转发该默认值。
def _max_output_tokens(model: str) -> int:
    m = model.lower()
    if "gemini-2.5" in m or "gemini-3" in m:
        # API 的上界是开区间，因此对 65536 token 家族，65535 是最大的合法请求。
        return 65_535
    # 1.5/2.0 家族上限为 8192。
    return 8_192


# 表示模型已产出内容但被拦截或扣留的完成原因。把其中任何一个伪装成 END_TURN 都会
# 静默结束本轮：没有截断警告、没有拒绝信号，界面永远不知道原因。
_BLOCKED_FINISH_REASONS = frozenset({
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
})


def _part_text(part) -> str | None:
    """当该 part 没有文本字段时，Part.text 会抛 ValueError（旧 SDK 则返回
    None）。"""
    try:
        return part.text
    except ValueError:
        return None


def _prompt_block_reason(feedback) -> str:
    """提示级拦截原因的名称，提示通过时返回 ''。

    Gemini 通过不返回候选并设置 ``prompt_feedback.block_reason`` 来拦截提示本身；
    候选级的完成原因映射永远看不到它，因此没有这个检查，本轮会以空的普通回复结束、
    没有拒绝信号。
    """
    reason = getattr(feedback, "block_reason", None)
    name = getattr(reason, "name", None) or (str(reason) if reason else "")
    if not name or name.upper() == "BLOCK_REASON_UNSPECIFIED":
        return ""
    return name


async def _prepend_chunk(first, rest):
    """在迭代 *rest* 之前，先产出已消费的首个数据块。"""
    if first is not None:
        yield first
    async for chunk in rest:
        yield chunk


class GoogleGeminiProvider(LLMProvider):
    """通过 google-genai SDK 调用 Google Gemini。"""

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash", base_url: str | None = None, url_mode: str = "base_url", context_window: int | None = None, ssl_verify: bool = False, thinking_enabled: bool | None = None, reasoning_effort: str | None = None) -> None:
        self._api_key = api_key
        self._model_name = model
        self._base_url = base_url
        self._ssl_verify = ssl_verify
        # 来自“设置 -> AI 模型”的逐配置覆盖；None = 名称匹配默认值。
        self._context_window_override = context_window
        # 缓存的客户端，首次请求时惰性构建并在 close() 中释放。SDK Client 持有
        # 必须关闭的 httpx 连接池，因此不能逐次调用重建。
        self._client_cache: genai.Client | None = None
        # include_thoughts 开关：显式的逐配置覆盖优先，否则取
        # AGENT_EXTENDED_THINKING（默认开启）；当模型/网关以 400 拒绝
        # ThinkingConfig 时也会关闭。reasoning_effort 仅 OpenAI 使用——这里接受
        # 它，使与供应方无关的凭证字典绝不会让某轮 TypeError。
        self._thinking_enabled = thinking_enabled if thinking_enabled is not None else thinking_enabled_by_env()

    def _client(self) -> genai.Client:
        """实例级客户端，只构建一次——google-genai 没有进程全局变量，且 Client
        内部的连接池本就设计为跨请求复用。"""
        if self._client_cache is not None:
            return self._client_cache
        http_options = genai_types.HttpOptions(
            **({"base_url": self._base_url.rstrip("/")} if self._base_url else {}),
            client_args={"verify": self._ssl_verify},
            # 总计 300 秒（HttpOptions.timeout 单位为毫秒；SDK 把它映射为
            # httpx/aiohttp 的逐请求超时）。SDK 默认值为 None，传输层会当作
            # 无限——一个黑洞端点会让本轮一直挂起到进程级取消。与 openai/
            # anthropic 供应方分阶段的 300 秒上限一致。
            timeout=300_000,
        )
        saved = {k: os.environ.pop(k) for k in _PROXY_KEYS if k in os.environ}
        try:
            self._client_cache = genai.Client(api_key=self._api_key, http_options=http_options)
        finally:
            os.environ.update(saved)
        return self._client_cache

    async def close(self) -> None:
        """释放缓存客户端的 httpx 连接池（每次运行后 Agent.close() 会等待
        本方法）。"""
        client = self._client_cache
        self._client_cache = None
        if client is not None:
            await client.aio.aclose()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def context_window(self) -> int:
        return self._context_window_override or _context_window(self._model_name)

    @property
    def supports_tool_calls(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return True

    def _to_gemini_contents(self, messages: list[Message]) -> tuple[str | None, list]:
        system = None
        contents = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content if isinstance(msg.content, str) else str(msg.content)
                continue
            if msg.role == "tool":
                part = {
                    "function_response": {
                        "name": msg.name or "",
                        "response": {"result": msg.content if isinstance(msg.content, str) else json.dumps(msg.content)},
                    }
                }
                # 并行工具调用会产生连续的工具消息。每个都不能变成独立的用户轮次
                # ——Gemini 拒绝相邻用户内容携带 function_response 部分造成的交替
                # 违规。当前一条用户内容只含 function_response 部分时，合并进它。
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and all("function_response" in p for p in contents[-1]["parts"])
                ):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            if msg.role == "assistant" and msg.tool_calls:
                parts = []
                if msg.content:
                    parts.append({"text": msg.content})
                for tc in msg.tool_calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append({
                        "function_call": {
                            "name": fn.get("name", ""),
                            "args": args,
                        }
                    })
                # 委派子智能体持久化的回复可能紧挨在其父级使用工具的最终消息之前；
                # 合并进那个 model 轮次，而不是发出相邻的一个。
                if contents and contents[-1]["role"] == "model":
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": "model", "parts": parts})
                continue
            role = "model" if msg.role == "assistant" else "user"
            if isinstance(msg.content, str):
                parts = [{"text": msg.content}]
            else:
                parts = []
                for part in msg.content:
                    if part.get("type") == "image":
                        parts.append({
                            "inline_data": {
                                "mime_type": part["media_type"],
                                "data": part["data"],
                            }
                        })
                    else:
                        parts.append({"text": part.get("text", "")})
            # 连续的同角色内容——运行中途的注入正好落在工具结果轮次之后，两个排队
            # 的注入背靠背出现，持久化注入在之后每轮都于合成工具行之后重建，委派
            # 子智能体持久化的回复重放到其父级之前。Gemini 拒绝相邻的用户或 model
            # 内容（"multiturn requests alternate between user and model"），因此
            # 把各部分合并进前一条内容，而不是开启新轮次。
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        return system, contents

    def _to_gemini_tools(self, tools: list[ToolDefinition]):
        declarations = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tools
        ]
        return [genai_types.Tool(function_declarations=declarations)]

    def _thinking_supported(self) -> bool:
        if not self._thinking_enabled:
            return False
        m = self._model_name.lower()
        return "gemini-2.5" in m or "gemini-3" in m

    def _disable_thinking(self, reason: str) -> None:
        if self._thinking_enabled:
            self._thinking_enabled = False
            import logging
            logging.getLogger(__name__).warning(
                "include_thoughts disabled for %s: %s", self._model_name, reason,
            )

    def _request_config(
        self, system: str | None, tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> genai_types.GenerateContentConfig:
        kwargs: dict = dict(
            max_output_tokens=min(max_tokens, _max_output_tokens(self._model_name)),
            temperature=temperature,
            system_instruction=system or None,
            tools=self._to_gemini_tools(tools) if tools else None,
        )
        # 没有 include_thoughts，API 根本不会返回思考部分——即使模型在思考，思考
        # 界面也会一直为空。
        if thinking and self._thinking_supported():
            kwargs["thinking_config"] = genai_types.ThinkingConfig(include_thoughts=True)
        return genai_types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _empty_result(
        model: str,
        *,
        finish_reason: str = "error",
        stop_reason: StopReason = StopReason.UNKNOWN,
    ) -> CompletionResult:
        return CompletionResult(
            message=Message(role="assistant", content="", tool_calls=None),
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            model=model,
            finish_reason=finish_reason,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _normalize_stop(fr_name: str, has_tool_calls: bool) -> StopReason:
        # finish_reason 可能是 MAX_TOKENS（触及输出上限）或 SAFETY/RECITATION
        # （内容被拦截）——把它伪装成普通 END_TURN 会静默结束本轮：没有截断警告、
        # 没有拒绝信号，界面永远不知道回复被切断。
        if has_tool_calls:
            return StopReason.TOOL_USE
        if fr_name == "MAX_TOKENS":
            return StopReason.MAX_TOKENS
        if fr_name in _BLOCKED_FINISH_REASONS:
            return StopReason.REFUSAL
        return StopReason.END_TURN

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        system, contents = self._to_gemini_contents(messages)
        client = self._client()
        config = self._request_config(system, tools, max_tokens, temperature)
        try:
            response = await client.aio.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            # 模型/网关拒绝了 ThinkingConfig——去掉它重试一次。
            if config.thinking_config is not None and "think" in str(exc).lower():
                self._disable_thinking(f"request rejected thinking_config: {exc}")
                response = await client.aio.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=self._request_config(system, tools, max_tokens, temperature, thinking=False),
                )
            else:
                raise
        if not response.candidates:
            if _prompt_block_reason(getattr(response, "prompt_feedback", None)):
                return self._empty_result(
                    self._model_name,
                    finish_reason="refusal",
                    stop_reason=StopReason.REFUSAL,
                )
            return self._empty_result(self._model_name)
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return self._empty_result(self._model_name)
        text_parts = []
        tool_calls = []
        for part in candidate.content.parts:
            # 思考部分在这里仅供展示（非流式路径没有界面通道）——它们不能泄漏到
            # 可见的助手文本中。
            if getattr(part, "thought", False):
                continue
            fc = part.function_call
            if fc:
                # Gemini 偶尔会发出名字为空的 function_call 部分（对应流式路径的
                # 续接块）。无名调用会在循环里以 "Unknown tool:" 出现——改为丢弃。
                if not (fc.name or "").strip():
                    continue
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    # 无参调用报告 args=None；dict(None) 会抛错。
                    "function": {"name": fc.name, "arguments": json.dumps(dict(fc.args or {}))},
                })
                continue
            text = _part_text(part)
            if text:
                text_parts.append(text)
        msg = Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls or None)
        usage = response.usage_metadata
        fr_name = str(getattr(candidate.finish_reason, "name", ""))
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_token_count if usage else 0,
                "completion_tokens": usage.candidates_token_count if usage else 0,
            },
            model=self._model_name,
            finish_reason=fr_name,
            stop_reason=self._normalize_stop(fr_name, bool(tool_calls)),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        system, contents = self._to_gemini_contents(messages)
        client = self._client()
        config = self._request_config(system, tools, max_tokens, temperature)
        try:
            chunk_stream = await client.aio.models.generate_content_stream(
                model=self._model_name,
                contents=contents,
                config=config,
            )
            # SDK 方法是一个只构建异步迭代器的协程；HTTP 请求在首次迭代时发出。
            # 预取一个数据块，使请求时的 ThinkingConfig 拒绝在这个 try 中浮现，
            # 而不是在下方循环内部。
            chunk_iter = chunk_stream.__aiter__()
            first_chunk = await chunk_iter.__anext__()
        except StopAsyncIteration:
            first_chunk = None
        except Exception as exc:
            if config.thinking_config is not None and "think" in str(exc).lower():
                self._disable_thinking(f"stream rejected thinking_config: {exc}")
                chunk_stream = await client.aio.models.generate_content_stream(
                    model=self._model_name,
                    contents=contents,
                    config=self._request_config(system, tools, max_tokens, temperature, thinking=False),
                )
                chunk_iter = chunk_stream.__aiter__()
                try:
                    first_chunk = await chunk_iter.__anext__()
                except StopAsyncIteration:
                    first_chunk = None
            else:
                raise
        tool_call_index = 0
        has_tool_calls = False
        last_usage = None
        # Gemini 会流式发出一个终端数据块，其 finish_reason 报告生成为何结束（输出
        # 上限时为 MAX_TOKENS，内容被拦截时为 SAFETY/RECITATION）——没有它，每个
        # 被切断的回复都会被读成正常的轮次结束。
        last_finish_reason = None
        # 提示级拦截原因（若 Gemini 拒绝了提示本身）。这种数据块不带候选，因此
        # 下方候选循环里没有任何东西会记录回复为何为空。
        prompt_block = ""
        # 每次调用索引的参数键指纹：无名续接块通过键重叠（见
        # _continuation_target_index）匹配到它所延续的调用，而不是假定为最近一次
        # 调用——并行调用的续接块会交错。
        seen_arg_keys: dict[int, set[str]] = {}
        async for chunk in _prepend_chunk(first_chunk, chunk_iter):
            # 在下方内容短路之前先捕获用量：Gemini 在终端块上报告 usage_metadata，
            # 该块带 finish_reason 但没有 parts——旧位置（`continue` 之后）丢弃了
            # 每个这种流的 token 计数。
            if chunk.usage_metadata:
                last_usage = chunk.usage_metadata
            block = _prompt_block_reason(getattr(chunk, "prompt_feedback", None))
            if block:
                prompt_block = block
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is not None:
                last_finish_reason = finish_reason
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                # include_thoughts 开启时，思考部分随可见内容一起（或在其之前）
                # 到达——把它们路由到思考通道。没有这个，它们会被误标为普通文本。
                if getattr(part, "thought", False):
                    thought_text = _part_text(part)
                    if thought_text:
                        yield StreamChunk(thinking=thought_text)
                    continue
                fc = part.function_call
                if fc:
                    name = (fc.name or "").strip()
                    args_json = json.dumps(dict(fc.args)) if fc.args else ""
                    if name:
                        # 一次函数调用的首个（或唯一）数据块。
                        seen_arg_keys[tool_call_index] = set(dict(fc.args)) if fc.args else set()
                        yield StreamChunk(
                            delta="",
                            tool_call_delta={
                                "index": tool_call_index,
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "function": {"name": name, "arguments": args_json},
                            },
                        )
                        # has_tool_calls 只应在确实发出增量时设置——索引 0 处
                        # 无名的 function_call 部分（下方跳过）作为不可解析被丢弃，
                        # 不能在没有东西可消费时报告 finish_reason="tool_calls"。
                        has_tool_calls = True
                        tool_call_index += 1
                    elif tool_call_index > 0:
                        # 续接块：Gemini 以空名字把较大参数拆到多个数据块——在匹配
                        # 调用的索引下发出，使循环合并这些片段。
                        chunk_args = dict(fc.args) if fc.args else {}
                        target = _continuation_target_index(
                            seen_arg_keys, chunk_args, tool_call_index - 1
                        )
                        if chunk_args and target in seen_arg_keys:
                            seen_arg_keys[target] |= set(chunk_args)
                        yield StreamChunk(
                            delta="",
                            tool_call_delta={
                                "index": target,
                                "function": {"name": "", "arguments": args_json},
                            },
                        )
                    continue
                text = _part_text(part)
                if text:
                    yield StreamChunk(delta=text)
        usage_dict = None
        if last_usage:
            usage_dict = {
                "prompt_tokens": getattr(last_usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(last_usage, "candidates_token_count", 0) or 0,
            }
        if has_tool_calls:
            yield StreamChunk(finish_reason="tool_calls", stop_reason=StopReason.TOOL_USE, usage=usage_dict)
        else:
            # 暴露终端块的 finish_reason，使循环能对截断/拒绝发出警告，而不是静默
            # 结束本轮。
            fr_name = str(getattr(last_finish_reason, "name", ""))
            if fr_name == "MAX_TOKENS":
                yield StreamChunk(finish_reason="max_tokens", stop_reason=StopReason.MAX_TOKENS, usage=usage_dict)
            elif fr_name in _BLOCKED_FINISH_REASONS or prompt_block:
                yield StreamChunk(finish_reason="refusal", stop_reason=StopReason.REFUSAL, usage=usage_dict)
            else:
                yield StreamChunk(finish_reason="stop", stop_reason=StopReason.END_TURN, usage=usage_dict)

    async def embed(self, text: str) -> list[float]:
        client = self._client()
        result = await client.aio.models.embed_content(
            model="text-embedding-004",
            contents=text,
        )
        return list(result.embeddings[0].values)
