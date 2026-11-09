"""对话历史压缩器（compressor）。

当对话历史的 Token 数超过阈值时，使用低档模型（model_low）对较早的消息做
摘要，使整体请求保持在模型上下文窗口与网关字节上限之内。本模块同时提供
Token 估算、传输字节估算、压缩预算计算，以及工具消息的就地截断与图片降级
等能力，是 Agent 控制上下文体积的核心。
"""
from __future__ import annotations

import json
import logging
import os
import re
import traceback
from functools import lru_cache

from .providers.base import LLMProvider, Message, ToolDefinition

COMPRESS_THRESHOLD_RATIO = 0.6
RECENT_TURNS_KEEP = 8
TOKENS_PER_WORD = 1.3
TOKENS_PER_CJK_CHAR = 0.6
# 摘要输入文本的总上限（自动路径）。对无界输入（每条消息 2000 字符 × 最多
# 200 条消息）调用非流式 complete()，必然超过调用方的 asyncio 超时，导致压缩
# 在恰恰最需要它的对话上每轮都降级为"只保留近期消息"。6 万字符约合 3.6 万
# Token——小到能在超时内完成摘要，大到能承载近期对话脉络。
SUMMARY_INPUT_MAX_CHARS = 60_000
# build_summary_pair 构建的摘要用户消息前缀标记。agent._degrade_request 据此
# 跳过对已摘要历史的再次摘要（对摘要再摘要会让上下文漂移）。
SUMMARY_MARKER = "[Earlier conversation summary"
# 计算历史预算时为生成预留的输出 Token。每个 Agent 配置中 max_tokens 默认
# 128000；在 128k 上下文窗口下把它全部预留，会让预算始终 <= 0，使 gpt-4o
# 级模型的自动压缩失效。16384 是 gpt-4o 的输出上限。
MAX_OUTPUT_RESERVE = 16384
# 单个供应方请求的硬字节上限（传输尺寸）。企业网关以 413 拒绝超大载荷；
# Token 启发式会漏掉 base64 图片和中日韩字符转义，因此字节守卫是兜底。处于
# 更低网关限制（如 nginx 默认 1MB）后的运维者可以调小它。
_ESCAPE_FACTOR = 1.15
_MESSAGE_OVERHEAD_BYTES = 64
_TOOL_OVERHEAD_BYTES = 64

_log = logging.getLogger("agent_core.compressor")


def _env_max_request_bytes() -> int:
    raw = os.environ.get("AGENT_MAX_REQUEST_BYTES", "")
    try:
        return int(raw) if raw else 8 * 1024 * 1024
    except ValueError:
        _log.warning("AGENT_MAX_REQUEST_BYTES 不是整数，使用默认值")
        return 8 * 1024 * 1024


MAX_REQUEST_BYTES = _env_max_request_bytes()

SUMMARY_PROMPT = """Summarize the conversation so far concisely. Preserve:
- Key facts and decisions made
- Important tool call results (file paths, URLs, data found)
- Unresolved questions or pending work
- Context needed to continue the conversation

Be factual and brief. Output only the summary, no preamble."""

_CJK_RE = re.compile(
    r"[一-鿿぀-ゟ゠-ヿ가-힣㐀-䶿　-〿]"
)


@lru_cache(maxsize=256)
def _estimate_tokens(text: str) -> int:
    if text.isascii():
        # 仅按词数会把无空白的 ASCII 载荷（压缩 JSON、base64、单行文件读取）
        # 塌缩成一个"词"，于是 500KB 的工具结果只算约 1 个 Token，同时逃过
        # 超大工具消息截断和历史压缩阈值。因此在估算之外，再保留每 4 字符
        # 折合 1 Token 的下限。
        return int(max(len(text.split()) * TOKENS_PER_WORD, len(text) / 4))
    cjk_chars = len(_CJK_RE.findall(text))
    cjk_tokens = int(cjk_chars * TOKENS_PER_CJK_CHAR)
    non_cjk_text = _CJK_RE.sub("", text)
    non_cjk_words = len(non_cjk_text.split())
    non_cjk_tokens = int(non_cjk_words * TOKENS_PER_WORD)
    return cjk_tokens + non_cjk_tokens


def _estimate_tokens_safe(text: str) -> int:
    """绝不缓存大载荷的 Token 估算。

    _estimate_tokens 的 lru_cache 以完整文本为键，会把可达数 MB 的工具结果
    （文件系统读取、知识页面）在进程生命周期内一直保留，而二分查找截断路径
    还会在上面缓存每个前缀子串。短文本继续走缓存；长文本则通过未缓存的原始
    函数估算。
    """
    if len(text) <= 4096:
        return _estimate_tokens(text)
    return _estimate_tokens.__wrapped__(text)


def _content_as_str(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))


def estimate_history_tokens(messages: list[Message]) -> int:
    """估算消息列表的总 Token 数。

    助手 tool_calls 的参数也要计入：文件/知识写入轮次携带约 10 万字符的参数
    载荷，而其内容只是一句话——若只算内容，compress_history 对这类对话永远
    不会触发，直到供应方直接拒绝超大请求。
    """
    total = 0
    for m in messages:
        total += _estimate_tokens_safe(_content_as_str(m.content))
        if m.tool_calls:
            total += _tool_calls_wire_bytes(m.tool_calls) // 4
    return total


def compression_budget(context_window: int, reserved_input_tokens: int, max_tokens: int) -> int:
    """预留输入和有上限的输出空间后，剩余的历史预算。

    每个 Agent 配置中 max_tokens 默认 128000——在 128k 窗口下把它全部预留，
    会让预算始终 <= 0，静默禁用 gpt-4o 级模型的自动压缩。输出预留以
    MAX_OUTPUT_RESERVE（gpt-4o 输出上限）封顶。
    """
    return context_window - reserved_input_tokens - min(max_tokens, MAX_OUTPUT_RESERVE)


def estimate_wire_bytes(text: str) -> int:
    """估算文本载荷序列化后的传输尺寸。

    HTTP JSON 序列化把中日韩字符转义为 \\uXXXX（每个 6 字节），因此中日韩
    密集载荷比其原始长度大得多——Token 启发式会严重低估。ASCII 文本因 JSON
    引号和转义膨胀约 15%。
    """
    if text.isascii():
        # ASCII 载荷无需扫描中日韩字符：每次字节守卫调用都会在 MB 级工具输出
        # 上运行此函数，而正则正是 ASCII 密集内容每次调用的主要开销。
        return int(len(text) * _ESCAPE_FACTOR)
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int((cjk * 6 + other) * _ESCAPE_FACTOR)


def _tool_calls_wire_bytes(tool_calls: list) -> int:
    """对 tool_calls 块做传输估算，而不整体 json.dumps。

    参数字符串占了绝大部分；每次估算都 dump 整个块，会为一个没人看的阈值
    数字反复转义数 MB 的中日韩字符。逐部分估算保持同样的保守余量（1.15
    转义系数覆盖了不再单独计数的 JSON 键与括号）。
    """
    total = 32
    for tc in tool_calls:
        if not isinstance(tc, dict):
            total += 64
            continue
        total += 32
        total += estimate_wire_bytes(str(tc.get("id") or ""))
        fn = tc.get("function")
        if isinstance(fn, dict):
            total += estimate_wire_bytes(str(fn.get("name") or ""))
            args = fn.get("arguments")
            total += estimate_wire_bytes(
                args if isinstance(args, str)
                else json.dumps(args or {}, ensure_ascii=True, default=str)
            )
        else:
            total += estimate_wire_bytes(str(fn))
    return total


def estimate_request_bytes(
    messages: list[Message],
    tool_defs: list[ToolDefinition] | None = None,
    tool_wire_bytes: int | None = None,
) -> int:
    """估算请求体序列化后的尺寸（线上传输字节数）。

    刻意保守：计入中日韩转义、base64 图片数据和工具调用 JSON——这些正是 Token
    启发式漏掉的部分。不使用 lru_cache：载荷可达数 MB（理由同
    _estimate_tokens_safe）。每次调用开销为 O(载荷)；供应方紧接着就会序列化
    同一载荷，因此这里不是瓶颈。

    tool_wire_bytes 用于跳过对 tool_defs 的重复估算（其 schema 在一轮内是静态
    的）：Agent 循环中的逐次字节守卫，会传入循环前一次性算好的值。
    """
    total = 0
    for m in messages:
        total += _MESSAGE_OVERHEAD_BYTES
        content = m.content
        if isinstance(content, str):
            total += estimate_wire_bytes(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    total += estimate_wire_bytes(str(part))
                elif part.get("type") == "image":
                    # 传输形式为 data:{media_type};base64,{data}——前缀是
                    # ASCII，base64 相对原始字节膨胀 4/3。
                    total += 24 + len(part.get("data") or "")
                else:
                    total += estimate_wire_bytes(part.get("text") or "")
        if m.tool_calls:
            total += _tool_calls_wire_bytes(m.tool_calls)
        if m.tool_call_id:
            total += len(m.tool_call_id) + 16
        if m.name:
            total += len(m.name) + 16
    if tool_wire_bytes is not None:
        return total + tool_wire_bytes
    for t in tool_defs or []:
        total += _TOOL_OVERHEAD_BYTES
        total += estimate_wire_bytes(t.name)
        total += estimate_wire_bytes(t.description)
        total += estimate_wire_bytes(json.dumps(t.parameters, ensure_ascii=True, default=str))
    return total


def truncate_oversized_tool_messages(messages: list[Message]) -> int:
    """就地截断超大工具消息，返回被截断的条数。

    通过对估算 Token 数做二分查找，把每条工具消息限制在 2000 Token 预算内
    （估算值随前缀长度单调，因此切点能感知中日韩/ASCII——固定字符比例会在
    中日韩密集内容上超限）。角色和 id 都不动，使助手与工具的配对保持完整。
    """
    truncated = 0
    for m in messages:
        if m.role != "tool":
            continue
        content = _content_as_str(m.content)
        if _estimate_tokens_safe(content) <= 2000:
            continue
        lo, hi = 0, len(content)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _estimate_tokens_safe(content[:mid]) <= 2000:
                lo = mid
            else:
                hi = mid - 1
        m.content = content[:lo] + "\n[... truncated ...]"
        truncated += 1
    return truncated


def demote_image_messages(messages: list[Message]) -> int:
    """把 base64 图片部分替换为轻量文本引用。

    视觉块是线上传输中最大的不可再减项：base64 让原始字节膨胀 4/3，且它们
    没有摘要路径。降级它们能以牺牲视觉为代价保住本轮——Agent 需要时仍可用
    `filesystem read_file` 取回文件。返回被降级的条数。只改动消息列表内容
    下的内容部分；角色、tool_calls 和 id 都保留。
    """
    demoted = 0
    for m in messages:
        if not isinstance(m.content, list):
            continue
        parts = []
        changed = False
        for part in m.content:
            if isinstance(part, dict) and part.get("type") == "image":
                media_type = part.get("media_type", "image")
                parts.append({
                    "type": "text",
                    "text": (
                        f"[Attachment image ({media_type}) removed from context to fit the "
                        "request size limit — use filesystem read_file on its path to "
                        "inspect it if needed]"
                    ),
                })
                demoted += 1
                changed = True
            else:
                parts.append(part)
        if changed:
            m.content = parts
    return demoted


def request_byte_breakdown(
    messages: list[Message],
    tool_defs: list[ToolDefinition] | None = None,
) -> dict[str, int]:
    """为超限错误消息提供各部分的传输字节数。

    把估算请求体拆成四个可操作的桶，让用户（或调小 AGENT_MAX_REQUEST_BYTES
    的运维者）看清什么占主导：系统提示词（承载完整知识负载）、附件图片数据、
    普通历史文本，以及工具 schema。
    """
    system = 0
    images = 0
    text = 0
    tools = 0
    for m in messages:
        if isinstance(m.content, str):
            content_bytes = estimate_wire_bytes(m.content)
        else:
            content_bytes = 0
            for part in m.content:
                if not isinstance(part, dict):
                    content_bytes += estimate_wire_bytes(str(part))
                elif part.get("type") == "image":
                    images += 24 + len(part.get("data") or "")
                else:
                    content_bytes += estimate_wire_bytes(part.get("text") or "")
        if m.role == "system":
            system += content_bytes
        else:
            text += content_bytes
        if m.tool_calls:
            text += _tool_calls_wire_bytes(m.tool_calls)
    for t in tool_defs or []:
        tools += _TOOL_OVERHEAD_BYTES
        tools += estimate_wire_bytes(t.name)
        tools += estimate_wire_bytes(t.description)
        tools += estimate_wire_bytes(json.dumps(t.parameters, ensure_ascii=True, default=str))
    return {"system": system, "images": images, "text": text, "tools": tools}


def force_compress_history(
    messages: list[Message],
    byte_limit: int,
    tool_defs: list[ToolDefinition] | None = None,
    tool_wire_bytes: int | None = None,
) -> str:
    """在调用供应方之前，对请求载荷强制施加字节上限。

    这里刻意不做 LLM 摘要：截断后仍超限的情况属于系统提示词（知识）、近期
    文本或附件——对早期段落做摘要并不能缩小这些，跳过 LLM 能让这条热路径
    守卫保持确定性，且不涉及超时/失败处理。返回 "ok"（未改动）、"truncated"
    （工具输出已缩小）或 "over_limit"。图片降级刻意不放在这里：它会丢弃视觉，
    属于异步降级链（agent._degrade_request）中 LLM 摘要 + 知识降级阶段之后，
    而非这个廉价的第一道截断处理。
    """
    if estimate_request_bytes(messages, tool_defs, tool_wire_bytes) <= byte_limit:
        return "ok"
    truncate_oversized_tool_messages(messages)
    if estimate_request_bytes(messages, tool_defs, tool_wire_bytes) <= byte_limit:
        return "truncated"
    return "over_limit"


def split_early_recent(messages: list[Message]) -> tuple[list[Message], list[Message]]:
    """把消息拆成（早期 early，近期 recent）两段以便压缩。

    `recent` 保留最后 RECENT_TURNS_KEEP 条消息。切片边界可能落在助手
    tool_calls 消息与其工具结果之间——工具消息绝不能位于请求开头（API 会
    拒绝没有前置助手 tool_calls 消息的工具消息），因此会把开头的工具消息从
    `recent` 弹出。它们的内容已保存在 `early` 的摘要中。
    """
    early = messages[:-RECENT_TURNS_KEEP]
    recent = messages[-RECENT_TURNS_KEEP:]
    # 工具消息绝不能位于请求开头（API 拒绝没有前置助手 tool_calls 的工具
    # 消息）——助手 tool_calls 消息也绝不能没有其工具结果而悬空（同样会被
    # 拒绝）。把整对悬空消息弹回 `early`，让工具内容能进入摘要而不是被静默
    # 丢弃。
    while recent and (
        recent[0].role == "tool"
        or (recent[0].role == "assistant" and recent[0].tool_calls)
    ):
        early.append(recent.pop(0))
    return early, recent


def early_history_lines(early: list[Message]) -> list[str]:
    """把早期段按消息格式化为逐行文本（每条上限 2000 字符）。

    这是共享构件：format_early_history 把它们拼接成自动摘要输入；手动压缩
    服务则把它们分块用于其 map-reduce 处理。
    """
    lines = []
    for m in early:
        role_label = m.role.upper()
        content = _content_as_str(m.content)
        if m.tool_calls:
            tool_names = [tc.get("function", {}).get("name", "?") for tc in m.tool_calls if isinstance(tc, dict)]
            content += f" [tools: {', '.join(tool_names)}]"
        if content.strip():
            lines.append(f"{role_label}: {content[:2000]}")
    return lines


def format_early_history(early: list[Message], max_chars: int | None = None) -> str:
    """从早期段构建摘要输入文本。

    max_chars 限制总输入：摘要调用是非流式的，无界输入（每条消息 2000 字符
    × 最多 200 条已加载消息）必然超过调用方的 asyncio 超时，导致压缩在恰恰
    最需要它的对话上降级为"只保留近期"。超限时保留尾部（最近的）——对延续
    对话而言，近期性最重要——并明确标注丢弃部分。
    """
    parts = early_history_lines(early)
    if max_chars is not None:
        total = sum(len(p) + 1 for p in parts)
        if total > max_chars:
            kept: list[str] = []
            size = 0
            for part in reversed(parts):
                # 即使最新一行单独就超限，也始终保留它——空的摘要输入会让
                # 压缩失效。
                if kept and size + len(part) + 1 > max_chars:
                    break
                kept.append(part)
                size += len(part) + 1
            kept.reverse()
            dropped = len(parts) - len(kept)
            return (
                f"[... {dropped} earlier messages omitted from the summary input "
                f"to stay within {max_chars} characters ...]" + "\n" + "\n".join(kept)
            )
    return "\n".join(parts)


def build_summary_pair(summary: str) -> list[Message]:
    """构建替换被压缩早期段的摘要标记消息对。

    摘要使用 "user" 角色，这样在会把多个系统消息拍平的供应方中，它不会
    覆盖 Agent 的系统提示词。
    """
    summary_msg = Message(
        role="user",
        content=f"{SUMMARY_MARKER} - this is automated context, not a user message]\n\n{summary}",
    )
    ack_msg = Message(
        role="assistant",
        content="Understood, I have the earlier conversation context.",
    )
    return [summary_msg, ack_msg]


async def compress_history(
    messages: list[Message],
    token_budget: int,
    provider: LLMProvider,
    force: bool = False,
) -> list[Message]:
    """历史过长时，把早期消息压缩成摘要。

    返回新的消息列表，包含：
    - 开头的一条系统摘要消息（若发生了压缩）
    - 原样保留的最近 RECENT_TURNS_KEEP 条消息

    ``force`` 会绕过 Token 阈值检查。当请求字节超限但 Token 未超限（中日韩
    转义、base64 图片）时设置它，使摘要仍能缩小 Token 启发式漏掉的多消息
    历史。短历史分支（<= RECENT_TURNS_KEEP）仍就地截断，而不调用 LLM。摘要
    输入以 SUMMARY_INPUT_MAX_CHARS 封顶，使调用能在调用方的 asyncio 超时内
    顺利完成。
    """
    total_tokens = estimate_history_tokens(messages)
    threshold = int(token_budget * COMPRESS_THRESHOLD_RATIO)

    if not force and total_tokens <= threshold:
        return messages

    # 消息太少无法拆分时，就地截断大的工具结果
    if len(messages) <= RECENT_TURNS_KEEP:
        truncate_oversized_tool_messages(messages)
        return messages

    early, recent = split_early_recent(messages)

    early_text = format_early_history(early, max_chars=SUMMARY_INPUT_MAX_CHARS)
    if not early_text:
        return messages

    # 使用低档模型（model_low）生成摘要
    summary_messages = [
        Message(role="system", content=SUMMARY_PROMPT),
        Message(role="user", content=f"Conversation to summarize:\n\n{early_text}"),
    ]

    try:
        response = await provider.complete(summary_messages, tools=None)
        summary = (response.message.content or "").strip()
        if not summary:
            # 空内容是失败信号——提示词级安全拦截或空拒绝——而不是摘要。
            # 在这里替换占位符，会用空内容顶替早期历史并让本轮剩余时间都
            # 丢失它；因此像下方异常路径一样保留完整历史。
            _log.warning("History summarization returned empty content; skipping compression")
            return messages
    except Exception as e:
        # 压缩是软性预算优化，绝不能成为数据丢失路径：瞬时的摘要失败
        # （429/5xx/网络）不能用占位符替换早期历史。本轮跳过压缩；下一轮
        # 带着完整历史重试。
        if provider.secret_values():
            # exc_info 会打印原始异常，而供应方错误可能回显 api_key（SDK
            # 认证错误会引用它；在 get_provider 拒绝头部不安全的密钥之前，
            # h11 会引用它）。因此改为先格式化再脱敏。
            _log.warning(
                "History summarization failed, skipping compression: %s",
                provider.scrub(traceback.format_exc()),
            )
        else:
            _log.warning("History summarization failed, skipping compression: %s", e, exc_info=True)
        return messages

    return build_summary_pair(summary) + recent
