"""工具注册表——按需构建工具实例。

带可选重型依赖（playwright、Pillow）的工具采用惰性导入。
"""
from __future__ import annotations

from .base import Tool, ToolContext

# 核心工具——始终可用
from .deliver_file import DeliverFileTool
from .filesystem import FilesystemTool
from .knowledge_rw import KnowledgeRWTool
from .memory_rw import MemoryRWTool
from .planner import PlannerTool
from .shell import ShellTool
from .sql_query import SqlQueryTool
from .web_fetch import WebFetchTool

# plan_task 是框架级行为（循环会把它拦截进任务模式），不是智能体定义可省略的普通
# 工具。它始终被注入，但绝不放在 _CORE_TOOLS 里——见 build_tool_registry。
_PLAN_TOOL = PlannerTool

_CORE_TOOLS: list[type[Tool]] = [
    FilesystemTool,
    KnowledgeRWTool,
    MemoryRWTool,
    WebFetchTool,
    PlannerTool,
    SqlQueryTool,
    ShellTool,
    DeliverFileTool,
]

# 可选工具——惰性导入，以免缺包时报错。
_OPTIONAL_TOOL_MODULES = {
    "browser_playwright": ("agent_core.tools.browser_playwright", "BrowserPlaywrightTool"),
    "chart_renderer":     ("agent_core.tools.chart_renderer",     "ChartRendererTool"),
    "code_executor":      ("agent_core.tools.code_executor",      "CodeExecutorTool"),
    "image_annotator":    ("agent_core.tools.image_annotator",    "ImageAnnotatorTool"),
    "focus_template":     ("agent_core.tools.focus_template",     "FocusTemplateTool"),
    "user_confirm":       ("agent_core.tools.user_confirm",       "UserConfirmTool"),
    "jira":               ("agent_core.tools.jira",               "JiraTool"),
    "confluence":         ("agent_core.tools.confluence",          "ConfluenceTool"),
    "github":             ("agent_core.tools.github",             "GitHubTool"),
    "kong":               ("agent_core.tools.kong",               "KongTool"),
    "api_request":        ("agent_core.tools.api_request",        "ApiRequestTool"),
    "secret_vault":       ("agent_core.tools.secret_vault",       "SecretVaultTool"),
    "test_generator":     ("agent_core.tools.test_generator",     "TestGeneratorTool"),
    "script_writer":      ("agent_core.tools.script_writer",      "ScriptWriterTool"),
    "scheduler":          ("agent_core.tools.scheduler",          "SchedulerTool"),
    "git_repo":           ("agent_core.tools.git_repo",           "GitRepoTool"),
    "repo_graph":         ("agent_core.tools.repo_graph",         "RepoGraphTool"),
    "skill_source":       ("agent_core.tools.skill_source",       "SkillSourceTool"),
    "delegate_agent":     ("agent_core.tools.agent_delegate",     "AgentDelegateTool"),
    "list_agents":        ("agent_core.tools.agent_delegate",     "ListAgentsTool"),
}


def known_static_tool_names() -> frozenset[str]:
    """注册表可解析的工具名，无需导入可选模块。

    用于标记前置元数据声明了不存在能力的智能体。可选工具贡献其目录键——仅为读取
    ``name`` 而导入它们会白白拉入 playwright，而 ``build_tool_registry`` 本来就是
    拿这些键去匹配声明。核心工具需实例化，因为其中一些的 ``name`` 是属性，从类上
    读取会得到属性对象而非字符串——使真实工具看起来未知。
    """
    return frozenset(cls().name for cls in _CORE_TOOLS) | frozenset(_OPTIONAL_TOOL_MODULES)


def _load_optional(name: str) -> type[Tool] | None:
    entry = _OPTIONAL_TOOL_MODULES.get(name)
    if entry is None:
        return None
    module_path, class_name = entry
    try:
        mod = __import__(module_path, fromlist=[class_name])
        return getattr(mod, class_name)
    except (ImportError, AttributeError):
        return None


def build_tool_registry(tool_names: list[str] | None = None) -> dict[str, Tool]:
    """实例化所请求的工具（未指定时实例化所有可用内置工具）。"""
    import logging
    _log = logging.getLogger("agent_core.tools.registry")

    registry: dict[str, Tool] = {}

    for cls in _CORE_TOOLS:
        instance = cls()
        if tool_names is None or instance.name in tool_names:
            if instance.name in registry:
                raise ValueError(f"Duplicate tool name: {instance.name!r}")
            registry[instance.name] = instance

    # plan_task 是框架级行为（循环把它拦截进任务模式），不是智能体定义可省略的普通
    # 工具——显式 tools 列表若忘了它，不能静默剥夺智能体的规划能力。它在白名单之后
    # 注入，因此绝不会让其他核心工具泄漏进来：只有 plan_task 本身豁免该列表。
    if tool_names is not None and "plan_task" not in registry:
        registry["plan_task"] = _PLAN_TOOL()

    for name in _OPTIONAL_TOOL_MODULES:
        if tool_names is not None and name not in tool_names:
            continue
        cls = _load_optional(name)
        if cls is not None:
            instance = cls()
            if instance.name in registry:
                raise ValueError(f"Duplicate tool name: {instance.name!r}")
            registry[instance.name] = instance

    if tool_names is not None:
        missing = set(tool_names) - set(registry.keys())
        if missing:
            _log.warning("Agent declares tools not found in registry: %s", sorted(missing))

    return registry
