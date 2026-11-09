"""工具抽象接口。"""
from __future__ import annotations

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from typing import Any, Callable

_log = logging.getLogger("agent_core.tools")

# 所有能让 HTTP(S) 流量走代理的拼写。四个 HTTP(S) 变量始终携带解析出的 URL；
# ALL_PROXY 拼写被丢弃，因为本代码库没有任何东西解析它们——一个只有主机环境知道
# 的值，会把 code_executor 路由到浏览器工具从不去的地方。
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
_PROXY_ENV_STALE_KEYS = ("ALL_PROXY", "all_proxy")


def resolve_proxy_url(injected: dict[str, str] | None = None) -> str:
    """解析出的出站代理——每个联网通道共用同一优先级。

    注入的逐轮设置优先于进程环境，HTTPS 在 HTTP 之前（大写环境变量处于同一档，
    小写是最后手段）。这里刻意不解析 NO_PROXY——子进程原样继承它，使 main.py 的
    绕过列表（localhost 加 LLM 主机）继续生效。空字符串表示“无代理”。
    """
    import os
    injected = injected or {}
    return (
        (injected.get("HTTPS_PROXY") or os.environ.get("HTTPS_PROXY"))
        or (injected.get("HTTP_PROXY") or os.environ.get("HTTP_PROXY"))
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
        or ""
    )


def apply_proxy_env(env: dict[str, str], url: str | None = None) -> dict[str, str]:
    """就地规范化子进程环境中的代理变量。

    省略 ``url`` 时，代理只从进程环境解析（用于脚本运行等无上下文的调用方）。用
    解析出的代理替换子进程本会继承的任何值，使沙箱代码（以及它通过读环境变量库
    启动的 Playwright）像浏览器工具一样接入网络。没有解析出代理时，所有拼写都被
    移除——这同时充当空环境占位符清理（空代理值会破坏原生工具中的 URI 解析器）。
    NO_PROXY 原样保留。
    """
    if url is None:
        url = resolve_proxy_url()
    for key in _PROXY_ENV_KEYS:
        if url:
            env[key] = url
        else:
            env.pop(key, None)
    for key in _PROXY_ENV_STALE_KEYS:
        env.pop(key, None)
    return env


class Tool(ABC):
    """所有智能体工具的抽象基类。"""

    # 注入系统提示词的、面向行为的使用提示（1-2 行）。聚焦何时优先/避免该工具，
    # 而非其参数 schema。
    prompt_hint: str = ""

    @property
    @abstractmethod
    def name(self) -> str:
        """LLM 工具调用中使用的工具名。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """给 LLM 看的、人类可读的描述。"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """工具输入参数的 JSON Schema。"""
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: "ToolContext") -> dict[str, Any]:
        """执行工具并返回结果字典。"""
        ...

    def to_definition(self):
        """转换为给 LLM 供应方的 ToolDefinition。"""
        from agent_core.providers.base import ToolDefinition
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolContext:
    """执行期间传给工具的运行时上下文。"""

    def __init__(
        self,
        project_id: str,
        project_fs_path: str,
        conversation_id: str,
        user_id: str,
        db_session=None,
        http_client=None,
        browser=None,
        project_context=None,
        current_turn: int = 0,
        current_task_id: str | None = None,
        knowledge_cache=None,
        session=None,
        framework_root: str | None = None,
        secret_key: str = "",
        session_memories: list | None = None,
        upload_file_lookup: Callable[[str], bytes | None] | None = None,
        upload_file_names: Callable[[], list[str]] | None = None,
        db_session_factory: Callable[[], Any] | None = None,
        app_base_url: str = "",
        app_root_path: str = "",
        app: Any = None,
    ) -> None:
        self.project_id = project_id
        self.project_fs_path = project_fs_path
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.db_session = db_session
        self.db_session_factory = db_session_factory
        self.http_client = http_client
        self.browser = browser
        self.project_context = project_context
        self.current_turn = current_turn
        self.current_task_id = current_task_id
        self.knowledge_cache = knowledge_cache
        self.session = session
        self.framework_root = framework_root
        self.secret_key = secret_key
        self.session_memories: list[dict] = session_memories if session_memories is not None else []
        # 公开的应用基础 URL（如 https://agent.agents-universe.com）。由 API 层从
        # APP_BASE_URL 注入，使工具能产出全限定的媒体 URL，LLM 可把它当作可用的
        # 下载地址引用。本地/开发/测试环境为空——工具退化为相对的 /api/media 路径。
        self.app_base_url = app_base_url or ""
        # 来自 APP_ROOT_PATH 的部署子路径（如 /agent），当浏览器必须访问
        # https://host/agent/api/... 时拼到 app_base_url 上。
        self.app_root_path = app_root_path or ""
        # 当运行从 Web 服务启动时的 FastAPI 应用。只有会自己启动无头智能体轮次的
        # 工具（调度工具的 run_now）才需要它；独立使用 agent-core 时为 None。
        self.app = app
        # 本次运行是否可暂停等待人工交互。无头运行（已发布的智能体）为 False——
        # 确认提示必须退化为可读错误，而不是阻塞等待一个没人能给出的答案。
        self.interactive: bool = True
        self.upload_file_lookup = upload_file_lookup
        self.upload_file_names = upload_file_names
        self.ssl_verify: bool = False
        self.browser_ssl_verify: bool = True
        self.integration_settings: dict[str, str] = {}
        self.api_request_no_confirm: bool = False
        self._tool_registry: dict[str, Tool] = {}
        self.http_client_no_proxy = None
        self._browser_lock = asyncio.Lock()
        # 本会话打开的每个页面（它自己的和每个任务克隆的），使 cleanup() 能全部
        # 关闭——见 copy_for_task。上下文以同样方式注册：显式 BrowserContext 持有
        # 自己的页面，且录制只在上下文关闭时写入磁盘，因此 cleanup() 必须在浏览器
        # 消失前关闭它们。
        self._browser_pages: list = []
        self._browser_contexts: list = []
        self._browser_context = None
        # record_start 进行时活动的录制状态（上下文/页面/视频/临时目录）；未录制时
        # 为 None。
        self._browser_recording: dict | None = None
        self.mcp_manager = None  # McpConnectionManager，由 attach_mcp_tools 惰性设置
        # `delegate_agent` / `list_agents` 工具的不透明委派状态，由 API 层
        # （api.services.delegation）构建。None 表示本次运行完全不能委派——无头的
        # agent-core 运行，或应用接线早于该功能的轮次。agent-core 从不内省它：调用
        # 时直接交回给 API。
        self.delegation: Any = None

    def copy_for_task(self, task_id: str, turn: int) -> "ToolContext":
        """用于并行任务隔离的浅拷贝。

        共享 browser、http_client、db_session、project_context、mcp_manager、
        _browser_lock、_tool_registry、delegation（全部 asyncio 安全或只读——委派
        上下文是冻结的，因此并行规划任务并发委派时不会破坏彼此的链）。
        拥有独立的 current_task_id / current_turn，使工具在并发执行时以正确的任务
        归属发出事件。
        """
        import copy

        clone = copy.copy(self)  # 浅拷贝——所有资源共享引用
        # 惰性资源槽（browser、http_client）通过属主引用共享：ensure_* 会写回会话
        # 的上下文，使并行任务克隆复用同一个浏览器/客户端，而不是各自启动一个——
        # 逐克隆的资源永远不会被关闭（cleanup() 只看到共享上下文的槽）。浏览器
        # PAGE 是例外：它是可变的导航状态，因此每个任务拥有自己的页面（注册在属主
        # 上以便清理）。
        clone._shared = self
        clone._browser_page = None
        clone._browser_context = None
        clone._browser_recording = None
        clone.current_task_id = task_id
        clone.current_turn = turn
        # 并发任务不能共享同一个 SQLAlchemy 异步会话：同一会话上交错的 execute 会
        # 破坏其状态机。提供工厂时，给每个任务独立会话——调用方
        # （_execute_single_task）在任务循环结束时关闭它。
        if self.db_session_factory is not None and self.db_session is not None:
            try:
                clone.db_session = self.db_session_factory()
            except Exception:
                _log.warning("db_session_factory failed, sharing parent session", exc_info=True)
        return clone

    async def ensure_browser(self):
        """惰性启动并返回共享的 Playwright Chromium 浏览器。"""
        if self.browser is not None:
            return self.browser
        async with self._browser_lock:
            if self.browser is not None:
                return self.browser
            # 惰性资源位于共享上下文上（见 copy_for_task）：任务克隆必须复用会话的
            # 浏览器，而不是每个任务启动一个 Chromium 进程，且 cleanup() 只关闭共享
            # 上下文的槽。
            owner = getattr(self, "_shared", None) or self
            if owner.browser is not None:
                self.browser = owner.browser
                return owner.browser
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            try:
                proxy_url = self.proxy_url()
                launch_kwargs: dict = {"headless": True}
                if proxy_url:
                    launch_kwargs["proxy"] = {"server": proxy_url}
                browser = await playwright.chromium.launch(**launch_kwargs)
            except Exception:
                await playwright.stop()
                raise
            owner._playwright = playwright
            owner.browser = browser
            self.browser = browser
            return browser

    _CREDENTIAL_KEYS = frozenset({
        "TOKEN", "SECRET", "PASSWORD", "COOKIE", "KEY", "CREDENTIAL", "AUTH",
    })

    def cfg(self, key: str, default: str = "") -> str:
        """读取配置值——优先注入设置而非 os.environ。

        凭证键绝不会从注入设置中返回；它们必须存于 user_tokens 并通过 get_token()
        取回。设置检查只匹配最后一个下划线段（因此 ATLASSIAN_AUTH_TYPE 允许，
        GIT_TOKEN 不允许）；os.environ 回退保留更宽的子串阻断以做纵深防御。
        """
        upper = key.upper()
        if upper.rsplit("_", 1)[-1] in self._CREDENTIAL_KEYS:
            return default
        from_settings = self.integration_settings.get(key)
        if from_settings:
            return from_settings
        if any(segment in upper for segment in self._CREDENTIAL_KEYS):
            return default
        import os
        return os.environ.get(key, default)

    def proxy_url(self) -> str:
        """解析出的出站代理——优先级见 resolve_proxy_url。"""
        return resolve_proxy_url(self.integration_settings)

    # 为把环境传给 LLM 生成代码的工具（code_executor、shell）构建子进程环境时，
    # 需要剥离的环境变量键。按最后一个下划线段或已知敏感前缀匹配。
    _ENV_DENY_SUFFIXES = _CREDENTIAL_KEYS | {"DRIVER", "URL", "Dsn".upper()}
    _ENV_DENY_PREFIXES = (
        "DB_", "DATABASE", "REDIS", "SECRET", "PASSWORD",
        "TOKEN", "CREDENTIAL", "AUTH", "API_KEY", "PRIVATE",
        # 云凭证前缀——AWS_REGION 之类键无害，但对 LLM 生成的代码，整体剥离是
        # 保守默认（缺少区域配置只会失败，绝不会泄漏）。
        "AWS_", "AZURE_", "GOOGLE_", "GCP_", "ALICLOUD_",
    )
    # 精确匹配键：PROJECTS_ROOT 会向 LLM 生成的代码暴露兄弟项目位置，使其能构造
    # 跨项目路径。下方凭证键的后缀能躲开后缀/前缀规则（ID、URI、CREDENTIALS、
    # PWD）。
    _ENV_DENY_EXACT = frozenset({
        "PROJECTS_ROOT",
        "AWS_ACCESS_KEY_ID",
        "SQLALCHEMY_DATABASE_URI",
        "DATABASE_URL",
        # 旧版完整连接字符串（.env 中记录的回退）——携带 user:password@host。
        # 其后缀 STRING 不匹配任何拒绝规则。
        "MSSQL_CONNECTION_STRING",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MYSQL_PWD",
        "PGPASSWORD",
        # SSH 代理套接字——掌握该路径让代码能用主机用户的密钥签名（如经转发代理
        # 认证的 git push），与本列表其余项剥离的能力同类。
        "SSH_AUTH_SOCK",
    })

    def safe_env(self, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """返回剥离了类凭证键之后的 os.environ。

        由 code_executor 和 shell 使用，使 LLM 生成的代码无法外泄 SECRET_KEY、
        数据库连接字符串等。
        """
        import os
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper in self._ENV_DENY_EXACT:
                continue
            if upper.rsplit("_", 1)[-1] in self._ENV_DENY_SUFFIXES:
                continue
            if any(upper.startswith(p) for p in self._ENV_DENY_PREFIXES):
                continue
            env[key] = value
        if extra:
            env.update(extra)
        return env

    # 所有能让 HTTP(S) 流量走代理的拼写（与上方模块级辅助函数共享）。
    _PROXY_ENV_KEYS = _PROXY_ENV_KEYS
    _PROXY_ENV_STALE_KEYS = _PROXY_ENV_STALE_KEYS

    def proxy_env(self, env: dict[str, str]) -> dict[str, str]:
        """就地规范化子进程环境中的代理变量。

        用解析出的代理替换子进程本会继承的任何值，使沙箱代码（以及它通过读环境
        变量库启动的 Playwright）像浏览器工具一样接入网络。没有解析出代理时，所有
        拼写都被移除——这同时充当空环境占位符清理（空代理值会破坏原生工具中的 URI
        解析器；shell 工具对其子进程也做同样处理）。NO_PROXY 原样保留。
        """
        return apply_proxy_env(env, self.proxy_url())

    @property
    def conversation_media_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / ".tmp" / "media" / self.conversation_id)

    def register_tool(self, tool: Tool) -> None:
        self._tool_registry[tool.name] = tool

    def get_tool(self, name: str) -> Tool | None:
        return self._tool_registry.get(name)

    def knowledge_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / "knowledge")

    def tests_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / "tests")

    async def cleanup(self) -> None:
        """会话结束时释放资源（浏览器、HTTP 客户端、playwright、MCP）。"""
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close_all()
            except Exception:
                _log.debug("mcp_manager close_all failed", exc_info=True)
            self.mcp_manager = None
        if self.http_client is not None:
            try:
                await self.http_client.aclose()
            except Exception:
                _log.debug("http_client close failed", exc_info=True)
            self.http_client = None
        if self.http_client_no_proxy is not None:
            try:
                await self.http_client_no_proxy.aclose()
            except Exception:
                _log.debug("http_client_no_proxy close failed", exc_info=True)
            self.http_client_no_proxy = None
        # 上下文会关闭自己的页面；先关闭它们还会终结任何进行中的视频（上下文消失
        # 时 Playwright 写出 .webm）。若留给 browser.close()，录制永远不会被写入。
        contexts = list(getattr(self, "_browser_contexts", None) or [])
        own_context = getattr(self, "_browser_context", None)
        if own_context is not None and own_context not in contexts:
            contexts.append(own_context)
        for ctx in contexts:
            try:
                await ctx.close()
            except Exception:
                _log.debug("browser context close failed", exc_info=True)
        self._browser_contexts = []
        self._browser_context = None
        # 上面关闭上下文会把进行中的视频终结到其临时目录。从未停止的录制按约定在
        # 轮次结束时丢弃——移除上下文刚写入的目录。
        rec = getattr(self, "_browser_recording", None)
        if isinstance(rec, dict) and rec.get("dir"):
            try:
                shutil.rmtree(rec["dir"], ignore_errors=True)
            except Exception:
                _log.debug("recording cleanup failed", exc_info=True)
        self._browser_recording = None
        pages = list(getattr(self, "_browser_pages", None) or [])
        own_page = getattr(self, "_browser_page", None)
        if own_page is not None and own_page not in pages:
            pages.append(own_page)
        for page in pages:
            try:
                await page.close()
            except Exception:
                _log.debug("browser page close failed", exc_info=True)
        self._browser_pages = []
        self._browser_page = None
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                _log.debug("browser close failed", exc_info=True)
            self.browser = None
        pw = getattr(self, "_playwright", None)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                _log.debug("playwright stop failed", exc_info=True)
            self._playwright = None
