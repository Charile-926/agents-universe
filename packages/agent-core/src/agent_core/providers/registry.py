"""供应方注册表——惰性导入，使缺失的可选 SDK 不会导致启动失败。"""
from __future__ import annotations

from typing import Any

from .base import LLMProvider

# 惰性导入可调用对象：只在实际使用某供应方时才加载其 SDK。这样其他包无需安装
# 所有 LLM SDK 即可导入 agent_core。
_PROVIDER_LOADERS: dict[str, Any] = {
    "anthropic": lambda: __import__(
        "agent_core.providers.anthropic_claude", fromlist=["AnthropicClaudeProvider"]
    ).AnthropicClaudeProvider,
    "openai": lambda: __import__(
        "agent_core.providers.openai", fromlist=["OpenAIProvider"]
    ).OpenAIProvider,
    "azure_openai": lambda: __import__(
        "agent_core.providers.openai", fromlist=["AzureOpenAIProvider"]
    ).AzureOpenAIProvider,
    "google_gemini": lambda: __import__(
        "agent_core.providers.google_gemini", fromlist=["GoogleGeminiProvider"]
    ).GoogleGeminiProvider,
}


def get_provider(provider_key: str, credentials: dict[str, Any]) -> LLMProvider:
    """根据供应方键和凭证字典实例化供应方。

    各供应方的凭证键：
      anthropic:     api_key、model、base_url（可选）、url_mode（可选：base_url|full_url）
      openai:        api_key、model、base_url（可选）、url_mode（可选：base_url|full_url）
      azure_openai:  api_key、endpoint、api_version（可选）、model（可选——同时也是
                     Azure 部署名；没有独立的部署键，凭证里多余的部署键会被静默忽略）
      google_gemini: api_key、model、base_url（可选）、url_mode（可选：base_url|full_url）
    所有供应方还接受 context_window（可选：对名称匹配默认值的逐配置覆盖；
    None = 按模型名自动匹配）、thinking_enabled（可选：扩展思考覆盖；
    None = 环境默认值 AGENT_EXTENDED_THINKING——anthropic/google_gemini 会采用，
    openai/azure_openai 接受但忽略，其思维链只解析）以及 reasoning_effort
    （可选："minimal"|"low"|"medium"|"high"；只发给 OpenAI 家族的推理模型，
    其他地方忽略）。
    """
    loader = _PROVIDER_LOADERS.get(provider_key)
    if loader is None:
        raise ValueError(
            f"Unknown LLM provider: {provider_key!r}. Available: {list(_PROVIDER_LOADERS)}"
        )
    api_key = credentials.get("api_key")
    if isinstance(api_key, str) and api_key:
        # 密钥会原样进入 HTTP 头（x-api-key / Bearer / api-key）。h11 遇到非法
        # 值会把它回显出来拒绝（"Illegal header value b'...'"）——该回显本身就是
        # 凭证，会出现在流式错误消息、日志和 task_failed 行里。拒绝构造；消息中
        # 不点名任何秘密。
        from ..tools._http import _header_value_problem
        problem = _header_value_problem(api_key)
        if problem:
            raise ValueError(
                f"API key for provider {provider_key!r} {problem} — "
                "re-save it in Settings → AI Models without it"
            )
    try:
        cls = loader()
    except ImportError as e:
        raise ImportError(
            f"Failed to load provider '{provider_key}': {e}. "
            f"Ensure the required SDK is installed."
        ) from e
    return cls(**credentials)


def default_context_window(provider_key: str, model: str) -> int:
    """供应方在无覆盖时按名称匹配使用的上下文窗口（token 数）。

    这是“设置 → AI 模型”里显示为预填/默认值的数字，也是配置未设置 context_window
    时运行时回退采用的值。惰性导入与 _PROVIDER_LOADERS 对应，因此仅调用本函数
    绝不会拉入 LLM SDK。
    """
    if provider_key == "anthropic":
        from .anthropic_claude import _context_window
    elif provider_key == "google_gemini":
        from .google_gemini import _context_window
    else:
        # openai 与 azure_openai 共用 OpenAI 名称表。
        from .openai import _context_window
    return _context_window(model)
