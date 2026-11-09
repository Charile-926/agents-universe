"""Shell 工具——面向 git 与文件操作的受限 Bash 执行。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..paths import is_within
from ..sandbox import (
    _EXECUTION_ENV_VARS,
    _GIT_CONFIG_ENV_RE,
    python_guard_env,
    spawn_in_new_session,
    split_logical_lines,
    terminate_process_tree,
    validate_command,
)
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)
_TIMEOUT = 30
_MAX_TIMEOUT = 300
# Playwright / npm 测试运行默认给满预算：浏览器启动、依赖检查和每用例 60 秒的
# 套件很容易超过 30 秒的通用默认，而导航中途的工具级终止对智能体呈现为连接失败
# 而非超时（失败读作“无法到达目标”）。对应服务端 spec 通道的宽裕额度（那里
# 540 秒）；300 是本工具自己的上限。显式 timeout_seconds 始终优先。
_PLAYWRIGHT_RUN_TIMEOUT = 300
# `npm run test:{slug}` 命令行里没有字面 "playwright"——也要匹配 npm 测试脚本
# 家族，但不匹配 `npm run typecheck`。
_PLAYWRIGHTISH_CMD = re.compile(r"playwright|\bnpm\s+(?:run\s+)?test\b", re.IGNORECASE)
_NPM_INSTALL_TIMEOUT = 120
_MAX_OUTPUT = 10_000
# 旧版共享 npm 缓存。仅在确实可写时使用——root 拥有的 /tmp/npm-cache（npm
# 的 bug）不得阻塞安装。
_NPM_CACHE_FALLBACK = "/tmp/npm-cache"

# Windows 上使用 Git Bash，使 Unix 命令（find、grep 等）正确工作。按顺序搜索；
# 第一个匹配优先。都找不到则回退到 cmd.exe。
_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

def _get_shell() -> list[str] | None:
    """返回给子进程用的 [shell, 标志]，None 则用 create_subprocess_shell 默认。"""
    if sys.platform != "win32":
        return None  # Linux/macOS：create_subprocess_shell 原生用 /bin/sh
    for candidate in _GIT_BASH_CANDIDATES:
        if Path(candidate).exists():
            return [candidate, "-c"]
    return None  # 回退到 cmd.exe

_SHELL_ARGS = _get_shell()


def redact_proxy_credentials(text: str, proxy_url: str) -> str:
    """屏蔽工具可见文本中带凭证的代理 URL（见 shell.execute）。

    惰性导入：code_executor 在模块级从本模块导入 redact_secrets，顶部导入会循环。
    """
    from .code_executor import redact_proxy_credentials as _redact

    return _redact(text, proxy_url)


def _redact_env_proxies(text: str, env: dict[str, str]) -> str:
    """屏蔽 npm 的 stderr 可能引用的带凭证代理 URL。

    ensure_node_deps 以 WARNING 记录 stderr 并把它返回给两个调用方——shell 工具
    （在返回路径上再次脱敏，对日志行已太晚）和 API 脚本执行器（把它存进运行日志）
    ——因此清洗在这里发生，携带凭证的子进程环境仍在作用域内。不带密码的值被
    redact_proxy_credentials 忽略；其余的过度脱敏是可接受的代价（乱码诊断绝不
    泄漏）。
    """
    for value in env.values():
        if "@" in value:
            text = redact_proxy_credentials(text, value)
    return text


def _default_timeout(command: str) -> int:
    """*command* 的默认工具超时——见 _PLAYWRIGHT_RUN_TIMEOUT。"""
    return _PLAYWRIGHT_RUN_TIMEOUT if _PLAYWRIGHTISH_CMD.search(command) else _TIMEOUT


def redact_secrets(text: str, secrets: dict[str, str]) -> str:
    """把每个解析出的密钥值替换为 '[REDACTED:<ENV_NAME>]'。

    仅精确子串匹配。在解码之后、截断之前应用，使跨越 _MAX_OUTPUT 边界的密钥被
    完整屏蔽。没有最小长度守卫：过度脱敏会搅乱输出但绝不泄漏，脱敏不足则是泄漏。
    Base64/转义变体不在范围内。
    """
    for name, value in secrets.items():
        if value:
            text = text.replace(value, f"[REDACTED:{name}]")
    return text


# 给不带 git 配置的标准容器用的合成 git 身份。仅对仓库（或全局/系统配置）未定义
# 的字段注入——git 按环境变量 > 配置文件解析身份，因此无条件注入会静默替换仓库
# 本地的 user.name/email。
_GIT_IDENTITY_DEFAULTS = {
    "GIT_AUTHOR_NAME": "Agents Universe",
    "GIT_AUTHOR_EMAIL": "agents-universe@localhost",
    "GIT_COMMITTER_NAME": "Agents Universe",
    "GIT_COMMITTER_EMAIL": "agents-universe@localhost",
}


def _repo_missing_git_identity(cwd: str) -> dict[str, str]:
    """返回缺失身份字段对应的合成 git 身份环境。

    探测 ``git config user.name`` / ``user.email``（合并本地 + 全局 + 系统）。非
    仓库目录报告无身份并拿到默认值，这无害——git commit 在那里本就会因其他原因
    失败。git 本身不可用时返回 {}。
    """
    identity: dict[str, str] = {}
    try:
        for key in ("name", "email"):
            proc = subprocess.run(
                ["git", "-C", cwd, "config", f"user.{key}"],
                capture_output=True, text=True, timeout=5,
            )
            configured = proc.returncode == 0 and bool(proc.stdout.strip())
            if not configured:
                env_name = f"GIT_AUTHOR_{key.upper()}"
                identity[env_name] = _GIT_IDENTITY_DEFAULTS[env_name]
                identity[f"GIT_COMMITTER_{key.upper()}"] = _GIT_IDENTITY_DEFAULTS[f"GIT_COMMITTER_{key.upper()}"]
    except Exception:
        return {}  # git 缺失或损坏——环境原样保留
    return identity


def _build_env(
    context: ToolContext,
    *,
    extra: dict[str, str] | None = None,
    git_identity: dict[str, str] | None = None,
) -> dict[str, str]:
    """构建把 JAVA_HOME/bin 放进 PATH 的子进程环境。

    用 safe_env() 剥离类凭证键（SECRET_KEY、数据库 URL 等），使 LLM 生成的命令
    无法外泄密钥。`extra` 在剥离之后合并——它携带从金库解析的密钥，其明文从不进入
    LLM 上下文（只注入子进程环境）。`git_identity` 只携带仓库未定义的身份字段。

    代理变量经过与 code_executor 和浏览器工具相同的 proxy_env() 规范化：经
    proxy_url() 解析一个 URL（先注入设置，再进程环境），写进全部四个 HTTP(S)
    拼写，丢弃未解析的 ALL_PROXY 残留，并清洗空的 .env 占位符（空的
    ``HTTPS_PROXY=`` 对 Python 是“无代理”，对 osemgrep 的 OCaml 解析器却是致命
    URI）。直接传原始 os.environ 会让 shell 子进程与沙箱对使用哪个代理产生分歧。
    """
    env = context.proxy_env(context.safe_env())
    java_home = env.get("JAVA_HOME", "")
    if java_home:
        java_bin = os.path.join(java_home, "bin")
        path = env.get("PATH", "")
        if java_bin not in path.split(os.pathsep):
            env["PATH"] = java_bin + os.pathsep + path
    # 标准容器不带 git 身份，因此 `git commit` 会以 "Please tell me who you are"
    # 失败。只填缺失字段——真实的仓库或全局配置（或显式环境变量）必须继续优先。
    if git_identity:
        env.update(git_identity)
    # 为该命令（或其子进程）启动的任何 Python 武装 sitecustomize 文件访问守卫。
    # 沿整个进程树继承。
    env.update(python_guard_env(context.project_fs_path))
    if extra:
        env.update(extra)
    return env


# env_refs 绝不能设置的名：执行控制黑名单（沙箱 _EXECUTION_ENV_VARS——“无论值
# 是什么，按名拒绝赋值”）加上 Python 文件访问守卫自己的键。_build_env 在
# python_guard_env 之后应用 env_refs，因此 PYTHONPATH 或 _AGENT_PROJECT_ROOT
# 条目会为命令启动的每个 `python` 静默解除 sitecustomize，BASH_ENV/GIT_PAGER
# 条目会在每次运行时把金库值当代码执行——正是命令前缀校验所持的不变量。
# GIT_CONFIG_COUNT/KEY_n/VALUE_n 家族无法枚举（名是带索引的），因此
# _resolve_env_refs 用沙箱正则匹配它们——与命令前缀通道完全一样。
_ENV_REF_DENYLIST: frozenset[str] = _EXECUTION_ENV_VARS | frozenset(python_guard_env(""))


async def _resolve_env_refs(
    context: ToolContext, env_refs: dict[str, dict]
) -> tuple[dict[str, str] | None, list[str], str | None]:
    """针对金库解析 env_refs（环境名 -> {ref, scope, environment}）。

    返回（已解析、缺失、错误）。scope='user' 只读 user_tokens；scope='project'
    读 project_secrets 并以 user_tokens 回退（与 api_request 一致）。快速失败：
    任何缺失引用都阻塞命令——部分无凭证运行会产生误导结果。缺失 db_session 会自然
    退化为缺失引用（get_*_optional -> None）。
    """
    from ._auth import get_secret_optional, get_token_optional

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for env_name, spec in env_refs.items():
        # 先做名称校验：execve 在第一个 '=' 处拆分 KEY=VALUE，因此名为 "PATH=x"
        # 会设置 PATH，尽管黑名单是精确匹配查找，而 NUL 会直接破坏 envp 条目。
        if (
            not isinstance(env_name, str)
            or not env_name
            or "=" in env_name
            or "\0" in env_name
            or env_name in _ENV_REF_DENYLIST
            or _GIT_CONFIG_ENV_RE.match(env_name)
        ):
            return None, [], (
                f"env_refs[{env_name!r}] is not an allowed environment "
                "variable name (execution-control and sandbox-guard "
                "variables cannot be set through env_refs)"
            )
        if not isinstance(spec, dict) or not isinstance(spec.get("ref"), str) or not spec["ref"]:
            return None, [], f"env_refs[{env_name!r}] must be an object with a string 'ref'"
        scope = spec.get("scope", "user")
        if scope not in ("user", "project"):
            return None, [], f"env_refs[{env_name!r}].scope must be 'user' or 'project'"
        if scope == "user":
            value = await get_token_optional(context, spec["ref"])
        else:
            value = await get_secret_optional(context, spec["ref"], environment=spec.get("environment"))
        if value is None:
            missing.append(spec["ref"])
        else:
            resolved[env_name] = value
    if missing:
        return None, missing, None
    return resolved, [], None


# 安全命令白名单。与沙箱校验器（sandbox.py 的 _DATA_COMMANDS / _FIRST_POS_DATA
# / _DATA_VALUE_OPTS）保持同步，后者已知道如何对这些命令的参数做路径检查。
# /opt/semgrep-venv/bin/semgrep：semgrep 在导入时硬性要求 mcp 1.29.0（与
# agent-core 的 mcp>=2.0.0 不兼容），因此装在它自己的 venv 里；这是其控制台脚本
# 被白名单放行的绝对路径。
# `export` 被接纳，因为 sandbox.py 像校验前导 VAR=value 前缀一样校验它的
# NAME=value 参数（PATH/LD_PRELOAD 仍拒绝）。
#
# 这里刻意不用原始字符串黑名单：覆盖整条命令行的正则会把 grep PATTERN（合法的
# 源码审计）或带引号字符串里的 "pip install" 标记出来。rm/sudo/curl/wget/
# apt-get/yum/xargs 只是不在白名单里；find -exec/-execdir/-ok/-delete 和
# `pip install` 由 validate_command 按上下文拒绝，它也为依赖解析接纳
# `python3 -m pip install --dry-run`。
_ALLOWED_CMDS = re.compile(
    r"^(git|ls|cat|grep|find|jq|echo|printf|pwd|head|tail|wc|sort|uniq|diff|"
    r"mkdir|cp|mv|touch|stat|file|date|basename|dirname|"
    r"which|whoami|uname|printenv|test|export|"
    r"sed|awk|gawk|cut|tr|"
    r"npx|npm|node|python3?|java|javac|mvn|/opt/semgrep-venv/bin/semgrep)"
    r"(?:\s|$)"
)
_WRAPPER_COMMAND = re.compile(r"^\./(?:mvnw|gradlew)(?:\s|$)")


def _check_allowlist_per_segment(command: str) -> str | None:
    """把复合管道中的每个命令段对照白名单校验。

    用 shlex 按 shell 的“链接”分隔符（; | && || &）拆分命令，再检查每段是否以
    允许的命令开头。括号在这里刻意不当作分隔符：``find`` 用 ``\\(`` 和 ``\\)``
    做表达式分组，posix 模式的 shlex 会剥离反斜杠，使其与 shell 子shell 括号无法
    区分。在它们上面拆分会破坏每个 ``find \\( ... \\)`` 命令。

    换行在 shell 里也是分隔符，但对 shlex 只是普通空白，因此命令先切成逻辑行：
    ``ls\\nrm -rf x`` 会被分词为单个 ``ls`` 段，其参数含 ``rm``，rm 会未经检查
    运行。Heredoc 正文是数据而非命令，被跳过。

    跳过前导 VAR=value 赋值以到达实际命令令牌。所有段都通过返回 None，否则返回
    第一个违规令牌。
    """
    # 只有链接运算符，不含 ( )——见 docstring。"|&" 是 bash 的带 stderr 管道
    # 运算符，shlex 把它作为一个令牌发出；没有它，`git|&rm -rf *` 的第二条命令
    # 从不被白名单检查。
    _chain_seps = frozenset({"&&", "||", ";;", ";", "|", "|&", "&"})

    for line, is_heredoc_body in split_logical_lines(command):
        if is_heredoc_body:
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None  # 格式错误——让 validate_command 报告解析错误

        segments: list[list[str]] = []
        current: list[str] = []
        for tok in tokens:
            if tok in _chain_seps:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            segments.append(current)

        for seg in segments:
            idx = 0
            # 跳过前导 VAR=value 赋值（如 FOO=bar cmd ...）
            while idx < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[idx]):
                idx += 1
            if idx >= len(seg):
                continue
            cmd_token = seg[idx]
            # 为包装命令剥离前导 ./
            check = _ALLOWED_CMDS.match(cmd_token) or _WRAPPER_COMMAND.match(cmd_token)
            if not check:
                return cmd_token
    return None


def _strip_env_prefix(command: str) -> str:
    """命令词之前的前导 VAR=value 赋值（FOO=bar npm ...）。"""
    seg = command.split()
    idx = 0
    while idx < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[idx]):
        idx += 1
    return " ".join(seg[idx:]) if idx else command


def _find_package_dir(cwd: str, project_root: str, command: str = "") -> Path | None:
    """定位 npm/npx 命令所属、拥有 package.json 的目录。

    在项目根停止的有界向上搜索，使嵌套在 monorepo 内的工作区（package.json 在
    工作区之外的祖先）无法在项目沙箱之外安装——或让其 node_modules 被 rmtree。
    <cwd>/tests/package.json 回退仅在命令提及 playwright 时适用（QA 生成的
    spec 在那里）。
    """
    cwd_path = Path(cwd)
    root = Path(project_root).resolve()
    search = cwd_path
    for _ in range(5):
        if (search / "package.json").is_file():
            return search
        if search == root or not search.is_relative_to(root):
            break
        parent = search.parent
        if parent == search:
            break
        search = parent
    if "playwright" in command:
        tests_candidate = cwd_path / "tests"
        if (tests_candidate / "package.json").is_file():
            return tests_candidate
    return None


def _command_npm_cache(command: str) -> str | None:
    """返回命令自身指定的 npm 缓存目录，没有则返回 None。

    识别前导 NPM_CONFIG_CACHE= / npm_config_cache= 环境前缀（两种大小写——shell
    工具自己的注入用小写，用户常用大写）和显式 --cache=<dir> 标志。这些是用户
    命令能同时为依赖安装阶段和命令本身覆盖缓存的唯一方式。
    """
    for token in command.split():
        m = re.match(r"^(?:npm_config_cache|NPM_CONFIG_CACHE)=(.+)$", token)
        if m:
            return m.group(1).strip().strip("'\"")
        m = re.match(r"^--cache=(.+)$", token)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def _npmrc_cache_dir(pkg_dir: Path) -> str | None:
    """从项目的 .npmrc 读取 cache=<dir>（值可能带引号）。"""
    npmrc = pkg_dir / ".npmrc"
    if not npmrc.is_file():
        return None
    try:
        for line in npmrc.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            m = re.match(r"^cache\s*=\s*(.+)$", line, re.IGNORECASE)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        return None
    return None


def _cache_dir_allowed(resolved: Path, pkg_dir: Path) -> bool:
    """缓存候选留在包目录或系统临时目录内时为 True。

    命令行和项目的 .npmrc 都不可信——仓库本身可自带一个。没有这道门，/etc 或
    ~/.config 之类绝对值会让 npm 在任意主机目录创建 _cacache，这些写入 Python
    运行时守卫看不到，因为 npm 作为独立进程运行。
    """
    try:
        return (
            is_within(pkg_dir, resolved)
            or is_within(tempfile.gettempdir(), resolved)
        )
    except OSError:
        return False


def _ensure_writable_cache(cache_dir: str) -> bool:
    """创建缓存目录（需要时）并确认它可写。

    仅 os.makedirs(exist_ok=True) 不是可写性检查：在已存在的 root 拥有目录上它
    静默成功，EACCES 只在 npm 之后试图在其中创建 _cacache 时才浮现。显式的
    os.access(W_OK) 探针把那个迟到失败转为干净地回退到下一个缓存候选。
    """
    try:
        os.makedirs(cache_dir, exist_ok=True)
        return os.access(cache_dir, os.W_OK)
    except OSError:
        return False


def _resolve_npm_cache_dir(pkg_dir: Path, command: str) -> str | None:
    """为安装阶段挑选可写的 npm 缓存目录，按优先级：

    1. 命令自身指定的缓存目录（NPM_CONFIG_CACHE= 环境前缀或 --cache= 标志），
    2. 项目 .npmrc 里的 cache=<dir>，
    3. 项目内默认（{pkg_dir}/.npm-cache），因位于沙箱内而始终可写，
    4. 旧版共享 /tmp/npm-cache——仅在确实可写时。

    相对缓存路径相对 pkg_dir 解析，与安装以 cwd=pkg_dir 运行时 npm 解释它们的方式
    一致。没有候选可用（磁盘满/沙箱损坏）时返回 None。
    """
    candidates = [
        _command_npm_cache(command),
        _npmrc_cache_dir(pkg_dir),
        str(pkg_dir / ".npm-cache"),
        _NPM_CACHE_FALLBACK,
    ]
    for cache_dir in candidates:
        if not cache_dir:
            continue
        # npm 会展开 .npmrc 值里的 ~/；包目录里字面的 '~' 目录并不是 npm 自己
        # 使用的缓存。
        cache_dir = os.path.expanduser(cache_dir)
        resolved = Path(cache_dir)
        if not resolved.is_absolute():
            resolved = pkg_dir / resolved
        # 包含检查在可写探针之前运行：探针会创建目录，因此被拒绝的候选不得先
        # 落地。
        if not _cache_dir_allowed(resolved, pkg_dir):
            continue
        if _ensure_writable_cache(str(resolved)):
            return str(resolved)
    return None


def _required_node_bins(pkg_dir: Path, command: str) -> list[str]:
    """返回缺失的已声明依赖及其 npm bin 垫片。"""
    try:
        import json
        package = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(package, dict):
        return ["valid package.json"]
    declared = {}
    for key in ("dependencies", "devDependencies"):
        values = package.get(key, {})
        if isinstance(values, dict):
            declared.update(values)
    requirements: list[tuple[str, Path, str]] = []
    needs_playwright = "@playwright/test" in declared or "playwright" in command
    needs_typescript = "typescript" in declared or re.search(r"\btsc\b|typecheck", command)
    needs_eslint = "eslint" in declared or re.search(r"\beslint\b|lint", command)
    if needs_playwright:
        # 裸 `playwright` 包也带 CLI，只声明它的项目从 `npm install` 永远拿不到
        # node_modules/@playwright/test——检查那个固定路径会以“required local
        # binaries are still missing after npm rebuild”阻塞每个命令。
        playwright_pkg = (
            "playwright"
            if "playwright" in declared and "@playwright/test" not in declared
            else "@playwright/test"
        )
        requirements.append(
            (playwright_pkg, pkg_dir / "node_modules" / playwright_pkg, "playwright")
        )
    if needs_typescript:
        requirements.append(("typescript", pkg_dir / "node_modules/typescript", "tsc"))
    if needs_eslint:
        requirements.append(("eslint", pkg_dir / "node_modules/eslint", "eslint"))
    missing = []
    for name, module_path, bin_name in requirements:
        if not module_path.exists():
            missing.append(name)
        bin_dir = pkg_dir / "node_modules/.bin"
        # Windows 的 npm 创建 .cmd 垫片；两种形式都接受。
        if not (bin_dir / bin_name).exists() and not (bin_dir / (bin_name + ".cmd")).exists():
            missing.append(f".bin/{bin_name}")
    return missing


async def _rm_dir(path: str) -> str | None:
    """在后台线程删除目录树并报告失败。"""
    import shutil
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, shutil.rmtree, path)
    except OSError as e:
        _log.warning("Failed to remove %s: %s", path, e)
        return f"Failed to clean up node_modules before npm install: {e}"
    return None


async def ensure_node_deps(
    command: str, cwd: str, project_root: str, env: dict[str, str],
    *, install_timeout: int = _NPM_INSTALL_TIMEOUT,
) -> str | None:
    """在运行命令前安装并校验本地 Node 依赖。

    由 shell 工具和 API 的脚本执行器（Playwright 运行）共享。Linux 容器里有意
    保留 npm 的 bin 链接。生成的 Playwright 测试调用本地 ``playwright`` 和
    ``tsc`` 垫片，因此只检查 node_modules 目录不够。
    """
    if not re.match(r"^(npx\b|npm\s+(?:run\b|test\b|install\b|i\b))", _strip_env_prefix(command)):
        return None

    # 向上搜索（在 _find_package_dir 中）必须在项目根停止。嵌套在 monorepo 内的
    # 项目工作区（package.json 在工作区之外的祖先）之前会让 npm 安装——以及下方
    # node_modules 的 rmtree——在项目沙箱之外运行。
    pkg_dir = _find_package_dir(cwd, project_root, command)
    if pkg_dir is None:
        return None

    required = _required_node_bins(pkg_dir, command)
    nm = pkg_dir / "node_modules"
    if nm.exists() and os.access(str(nm), os.W_OK) and not required:
        return None
    if nm.exists() and not os.access(str(nm), os.W_OK):
        _log.info("Removing unwritable node_modules in %s", pkg_dir)
        cleanup_error = await _rm_dir(str(nm))
        if cleanup_error:
            return cleanup_error

    if sys.platform != "win32":
        npm_cache = _resolve_npm_cache_dir(pkg_dir, command)
        if npm_cache is None:
            return "Failed to prepare npm cache: no writable cache directory found"
        # 缓存值可来自项目的 .npmrc（可能随克隆仓库一起到达的文件）或命令里的
        # 环境前缀。原样插值会让 `cache=/tmp/$(id)` 在启动本次安装的 shell 里运行
        # 命令替换。
        prefix = f"npm_config_cache={shlex.quote(npm_cache)} "
    else:
        prefix = ""
    lockfile = pkg_dir / "package-lock.json"
    # npm ci 会拒绝未反映到 package-lock.json 的 package.json 变更。测试生成器
    # 可能给现有脚手架添加脚本/依赖，因此在锁跟上之前用 npm install。
    use_ci = lockfile.is_file() and lockfile.stat().st_mtime >= (pkg_dir / "package.json").stat().st_mtime
    install_cmd = f"{prefix}npm {'ci' if use_ci else 'install'} --prefer-offline"
    _log.info("Installing Node.js deps in %s", pkg_dir)

    async def _run_install(cmd: str) -> tuple[int | None, str]:
        try:
            if _SHELL_ARGS:
                proc = await asyncio.create_subprocess_exec(
                    *_SHELL_ARGS, cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                    **spawn_in_new_session(),
                )
            else:
                # 与 exec 分支相同的独立会话保证：POSIX 上运行的是这个 shell 分支，
                # 没有它，子进程留在服务器的进程组里——超时/中止时
                # terminate_process_tree 的 killpg 会 SIGKILL uvicorn 自身。
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                    **spawn_in_new_session(),
                )
        except OSError as exc:
            _log.warning("Failed to start npm dependency install in %s: %s", pkg_dir, exc)
            return None, f"Failed to start npm dependency install: {exc}"
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=install_timeout)
        except asyncio.TimeoutError:
            terminate_process_tree(proc)
            await proc.wait()
            _log.warning("npm dependency install timed out in %s", pkg_dir)
            return -1, f"npm dependency install timed out after {install_timeout}s"
        except asyncio.CancelledError:
            terminate_process_tree(proc)
            await proc.wait()
            raise
        # 截断之前脱敏：该文本（下方）以 WARNING 记录并返回给原样呈现它的调用方。
        # npm 会引用它无法到达的代理，两个调用方的环境都携带带凭证的 URL。
        return proc.returncode, _redact_env_proxies(
            stderr.decode(errors="replace"), env
        )[:_MAX_OUTPUT]

    returncode, stderr_text = await _run_install(install_cmd)
    if returncode in (None, -1):
        return stderr_text
    if returncode != 0:
        # ENOTEMPTY 意味着之前的安装留下了损坏的 node_modules。先擦除并重试一次
        # 再放弃。
        if "ENOTEMPTY" in stderr_text and nm.exists():
            _log.warning("npm ENOTEMPTY in %s - removing node_modules and retrying", pkg_dir)
            cleanup_error = await _rm_dir(str(nm))
            if cleanup_error:
                return cleanup_error
            returncode, stderr_text = await _run_install(f"{prefix}npm install")
            if returncode in (None, -1):
                return stderr_text
        if returncode != 0:
            _log.warning("npm dependency install failed (exit=%d) in %s: %s", returncode, pkg_dir, stderr_text[:500])
            return f"npm dependency install failed (exit code {returncode}): {stderr_text[:1000]}"

    missing = _required_node_bins(pkg_dir, command)
    if missing:
        # 当模块已存在但链接悬空或从未创建时，npm install 可能跳过重新链接
        # .bin 垫片（中断的安装、不可写 node_modules、bin-links 关闭）。
        # `npm rebuild` 无需重新安装即可重新生成这些链接——在宣布配置损坏前先试
        # 一次。
        _log.info("Rebuilding npm bin links in %s (missing: %s)", pkg_dir, ", ".join(missing))
        rebuild_returncode, rebuild_stderr = await _run_install(f"{prefix}npm rebuild")
        if rebuild_returncode not in (None, 0, -1):
            _log.warning(
                "npm rebuild failed (exit=%d) in %s: %s",
                rebuild_returncode, pkg_dir, rebuild_stderr[:500],
            )
        missing = _required_node_bins(pkg_dir, command)
        if missing:
            return (
                "npm dependency install completed but required local binaries are still missing "
                "after `npm rebuild`: " + ", ".join(missing)
                + ". Check that npm bin-links are enabled (`npm config get bin-links`) and that "
                "node_modules is writable."
            )
    return None


# Node Playwright 运行的浏览器预检预算。与 API 的 spec 运行器（scripts.py）
# 共享——两个通道一个数字、一种行为。
PLAYWRIGHT_BROWSER_TIMEOUT = 180

# 预检成功缓存：键 = 包目录 | 已安装 playwright 版本 | PLAYWRIGHT_BROWSERS_PATH。
# 升级 @playwright/test 的 npm 安装或变更的浏览器路径会产生不同键，因此检查重新
# 运行。同版本下进程中途删除浏览器不会重新触发——作为跳过每条命令上 npx 的代价
# 接受。
_browsers_verified: dict[str, str] = {}


def _declares_playwright(pkg_dir: Path) -> bool:
    """包依赖 Playwright（@playwright/test 或 playwright）时为 True。"""
    try:
        import json
        package = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(package, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        values = package.get(key, {})
        if isinstance(values, dict) and ("@playwright/test" in values or "playwright" in values):
            return True
    return False


def _installed_playwright_version(pkg_dir: Path) -> str | None:
    """本地安装的 Playwright 运行器版本，无法读取时为 None。

    无版本刻意不匹配缓存键的空哨兵：版本无法读取的包必须每次都预检。
    """
    import json
    for name in ("@playwright/test", "playwright"):
        manifest = pkg_dir / "node_modules" / name / "package.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = data.get("version") if isinstance(data, dict) else None
        if isinstance(version, str) and version:
            return version
    return None


async def ensure_playwright_browsers(
    command: str, cwd: str, project_root: str, env: dict[str, str],
    *, timeout: int = PLAYWRIGHT_BROWSER_TIMEOUT,
) -> str | None:
    """Node 运行前校验/安装本地 @playwright/test 的 chromium。

    由 shell 工具和 API 的 spec 运行器共享——同样的预检、同样的预算。容器把
    PYTHON Playwright 的 chromium 烤进 PLAYWRIGHT_BROWSERS_PATH；脚手架锁定的
    @playwright/test 可能期望另一个修订，没有这一步会以 Playwright 的
    "Executable doesn't exist" 浮现。无事可做（非 Playwright 命令、无本地垫片、
    或热缓存）或成功时返回 None；失败时返回错误字符串。安装超过 *timeout* 时
    ——在杀掉进程树之后——抛 asyncio.TimeoutError。
    """
    if not _PLAYWRIGHTISH_CMD.search(command):
        return None
    pkg_dir = _find_package_dir(cwd, project_root, command)
    if pkg_dir is None:
        return None
    if "playwright" not in command and not _declares_playwright(pkg_dir):
        return None
    # `npx --no-install` 需要本地垫片；没有它，用户命令本就会以自己的消息失败
    # （ensure_node_deps 负责那条路径）。
    bin_dir = pkg_dir / "node_modules" / ".bin"
    if not (bin_dir / "playwright").exists() and not (bin_dir / "playwright.cmd").exists():
        return None

    version = _installed_playwright_version(pkg_dir)
    cache_key = "|".join((
        str(pkg_dir.resolve()),
        version or "",
        env.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ))
    if version and cache_key in _browsers_verified:
        return None

    cmd = "npx --no-install playwright install chromium"
    try:
        if _SHELL_ARGS:
            proc = await asyncio.create_subprocess_exec(
                *_SHELL_ARGS, cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                **spawn_in_new_session(),
            )
        else:
            # 同 _run_install 的独立会话保证：没有它，子进程共享服务器进程组，超时
            # 时 terminate_process_tree 的 killpg 会 SIGKILL uvicorn 自身。
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                **spawn_in_new_session(),
            )
    except OSError as exc:
        _log.warning("Failed to start browser preflight in %s: %s", pkg_dir, exc)
        return f"Failed to start browser preflight: {exc}"
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        terminate_process_tree(proc)
        await proc.wait()
        _log.warning("Browser preflight timed out in %s", pkg_dir)
        raise
    except asyncio.CancelledError:
        terminate_process_tree(proc)
        await proc.wait()
        raise
    if proc.returncode == 0:
        if version:
            _browsers_verified[cache_key] = "1"
        return None
    stderr_text = _redact_env_proxies(stderr.decode(errors="replace"), env)
    _log.warning(
        "Browser preflight failed (exit=%s) in %s: %s",
        proc.returncode, pkg_dir, stderr_text[:500],
    )
    return f"Browser preflight failed (exit code {proc.returncode}): {stderr_text[:1000]}"


class ShellTool(Tool):
    name = "shell"
    prompt_hint = (
        "Allowed commands: git, ls, cat, grep, find, jq, sed, awk, cut, tr, "
        "echo, printf, pwd, head, tail, wc, sort, uniq, diff, mkdir, cp, mv, "
        "touch, stat, file, date, basename, dirname, which, whoami, uname, "
        "printenv, test, export, npx, npm, node, python, java, javac, mvn, ./mvnw, ./gradlew. "
        "Set environment variables with VAR=value prefixes or `export VAR=value`. "
        "Never use `cd` - use the `cwd` parameter instead. `git clone` is blocked - "
        "use the `git_repo` tool to clone repositories. "
        "ALL paths must be relative to the project root - absolute paths, ~, and ../ "
        "are rejected. If a command is rejected, switch to a dedicated tool instead of "
        "rephrasing. Playwright/npm test commands run dependency + browser setup first "
        "(self-healing); use shell for .spec.ts runs only as a last-resort fallback - "
        "ad-hoc browser probes belong to browser_playwright / code_executor python-playwright."
    )
    description = (
        "Run shell commands (bash) in a restricted environment. "
        "Never use `cd` - pass the working directory via the `cwd` parameter instead. "
        "`git clone` is blocked in shell - clone repositories with the `git_repo` tool. "
        "Allowed: git, ls, cat, grep, find, jq, echo, printf, pwd, head, tail, wc, "
        "sort, uniq, diff, mkdir, cp, mv, touch, stat, file, date, basename, dirname, "
        "which, whoami, uname, printenv, test, export, sed, awk, gawk, cut, tr, "
        "npx, npm, node, python, java, javac, mvn, ./mvnw, ./gradlew, "
        "/opt/semgrep-venv/bin/semgrep (static analysis; the only absolute command path). "
        "Every command in a compound pipeline (separated by ; | && ||) must be in the allowlist. "
        "All paths must stay inside the current project: absolute paths, '~', and '..' segments are rejected "
        "for file arguments, and redirection targets are validated the same way (the system temp dir is also allowed). "
        "Data arguments are NOT path-checked: commit messages (git -m), grep patterns, python -c code, and similar "
        "option values may freely contain '/', '~', or '..'. "
        "git supports common subcommands (status/diff/log/show/add/commit/push/pull/fetch/checkout/switch/restore/"
        "branch/tag/stash/blame/reset/rm/mv/revert/rebase/clean/cherry-pick/apply/am/config/submodule/worktree/remote "
        "and more); --git-dir/--work-tree/--exec-path are not allowed, git config is repo-local only (no --global/--system), "
        "and git clone is blocked. A default git identity is injected so commits work out of the box. "
        "python runs under a runtime file-access guard: scripts and 'python -m pytest' work normally, "
        "but reading or writing files outside the project is denied; -S/-I/-E flags are rejected. "
        "Parallel library tooling (multiprocessing.Pool, e.g. 'python3 -m detect_secrets scan') works: "
        "the guard admits multiprocessing's own worker fork, while direct os.fork() calls stay blocked. "
        "node does not support -e/--eval/-p/--print; write a script file and run it instead. "
        "Command substitution ($(...) or backticks) is not allowed — compute values in a first command, then use them. "
        "Blocked: rm -rf, sudo, curl, wget, find -delete, and pip install — except "
        "'python3 -m pip install --dry-run', which is the dependency-resolution "
        "channel (for pip_audit's degradation path). "
        "Secrets injected via env_refs are resolved server-side and never appear in returned output; "
        "the command fails without running if any referenced secret is missing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "cwd": {"type": "string", "description": "Working directory (relative to project root)"},
            "timeout_seconds": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 300,
                "description": (
                    "Command timeout in seconds (capped at 300). Defaults to "
                    "300 for playwright / npm test runs and 30 otherwise — "
                    "omit it rather than passing 30 for browser suites."
                ),
            },
            "env_refs": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Vault service_key to resolve (e.g. 'qa:login:password')"},
                        "scope": {
                            "type": "string", "enum": ["user", "project"], "default": "user",
                            "description": "'user' = user key vault (user_tokens); 'project' = project secrets with user_tokens fallback",
                        },
                        "environment": {
                            "type": "string",
                            "description": "Environment qualifier, used with scope='project'",
                        },
                    },
                    "required": ["ref"],
                },
                "description": (
                    "Resolve vault secrets into subprocess env vars, e.g. "
                    '{"APP_PASSWORD": {"scope": "user", "ref": "qa:login:password"}}. '
                    "Resolved plaintext values are redacted from all returned output. "
                    "The command fails without running if any ref is missing."
                ),
            },
        },
        "required": ["command"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        command = params["command"].strip()

        # 复合管道中的每个命令段都必须在白名单内。
        bad_token = _check_allowlist_per_segment(command)
        if bad_token is not None:
            return {"error": f"Command not in allowlist. First unallowed token: {bad_token!r}"}

        timeout_value = params.get("timeout_seconds", _default_timeout(command))
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, int):
            return {"error": "timeout_seconds must be an integer between 1 and 300"}
        if not 1 <= timeout_value <= _MAX_TIMEOUT:
            return {"error": f"timeout_seconds must be between 1 and {_MAX_TIMEOUT}"}

        # 解析工作目录（必须留在项目内）
        cwd = context.project_fs_path
        if params.get("cwd"):
            base = Path(context.project_fs_path).resolve()
            cwd_path = (base / params["cwd"]).resolve()
            if not cwd_path.is_relative_to(base):
                return {"error": "Working directory must be within project scope"}
            cwd = str(cwd_path)

        # 针对跨项目逃逸的令牌级路径/命令校验（'..' 段、绝对路径、重定向、
        # git -C/--git-dir 等）。
        reject_reason = validate_command(
            command, cwd=Path(cwd), project_root=Path(context.project_fs_path)
        )
        if reject_reason:
            return {"error": f"Command blocked for safety: {reject_reason}"}

        # 在任何副作用（npm install）之前把金库密钥解析进子进程环境——引用缺失的
        # 命令绝不能运行，哪怕部分运行。解析值从不进入 LLM 上下文。
        env_refs = params.get("env_refs")
        resolved_env: dict[str, str] = {}
        if env_refs:
            resolved_env, missing, ref_error = await _resolve_env_refs(context, env_refs)
            if ref_error:
                return {"error": ref_error}
            if missing:
                return {
                    "error": (
                        "env_refs unresolved — missing vault secrets: "
                        f"{missing}. Ask the user to save them (user_confirm "
                        "secret mode or secret_vault save), then retry the command."
                    ),
                    "missing_service_keys": missing,
                }

        # 在有 package.json 但无可用 node_modules 的目录运行 npx/npm 命令时，
        # 自动安装 Node.js 依赖。
        install_error = await self._ensure_node_deps(command, cwd, context)
        if install_error is None:
            # 浏览器预检：与服务器 spec 运行器运行的同一个，使 shell 回退不会因
            # 缺失/错误修订的 chromium 而死。严格在依赖阶段之后运行——
            # `npx --no-install` 需要该阶段刚保证的垫片。
            install_error = await self._ensure_playwright_browsers(command, cwd, context)
        if install_error:
            # npm 自己的 stderr 可能引用它无法到达的代理。
            install_error = redact_proxy_credentials(install_error, context.proxy_url())
            return {"error": redact_secrets(install_error, resolved_env)}

        # 注入 npm 缓存环境，避免容器里 /.npm 的权限错误。
        try:
            command = self._inject_npm_cache_env(command, cwd, context.project_fs_path)
        except OSError as exc:
            return {"error": f"Failed to prepare npm cache: {exc}"}

        # 只填仓库未配置的 git 身份字段（绝不覆盖仓库本地或全局 git 身份）。
        git_identity = await asyncio.to_thread(_repo_missing_git_identity, cwd)
        env = _build_env(context, extra=resolved_env or None, git_identity=git_identity)
        try:
            if _SHELL_ARGS:
                proc = await asyncio.create_subprocess_exec(
                    *_SHELL_ARGS, command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    # POSIX 上用独立会话，使后台孙进程在超时/中止时随进程树终止。
                    **spawn_in_new_session(),
                )
            else:
                # POSIX 上的独立会话（这个分支就是 POSIX 分支）：没有它，子进程共享
                # 服务器进程组，超时处理器的 killpg 会 SIGKILL uvicorn 自身。
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    **spawn_in_new_session(),
                )
        except OSError as exc:
            return {"error": f"Failed to start shell command: {exc}"}
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_value)
        except asyncio.TimeoutError:
            terminate_process_tree(proc)
            await proc.wait()
            return {"error": f"Command timed out after {timeout_value}s"}
        except asyncio.CancelledError:
            # 任务取消（用户中止 / 会话关闭）：子进程不得带着副作用（git push、
            # npm install、写入）继续运行。
            terminate_process_tree(proc)
            await proc.wait()
            raise

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        # 截断之前脱敏，使跨越 _MAX_OUTPUT 边界的密钥被完整屏蔽，然后再截断。
        # 代理凭证走与 code_executor 输出相同的路径：.env.example 承诺它们在任何
        # 工具输出或日志之前被屏蔽，否则打印自身环境（printenv HTTPS_PROXY）或
        # 回显代理错误的子进程会把它们交给模型。
        proxy_url = context.proxy_url()
        stdout_str = redact_proxy_credentials(stdout_text, proxy_url)
        stderr_str = redact_proxy_credentials(stderr_text, proxy_url)
        stdout_str = redact_secrets(stdout_str, resolved_env)[:_MAX_OUTPUT]
        stderr_str = redact_secrets(stderr_str, resolved_env)[:_MAX_OUTPUT]
        if proc.returncode != 0:
            _log.warning("shell command failed (exit=%d): %r\nstderr: %s", proc.returncode, command, stderr_str[:500])
        return {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "exit_code": proc.returncode,
        }

    # 与 API 脚本执行器共享的模块级实现；保留为属性，使现有调用方（及测试）继续
    # 可用。
    _strip_env_prefix = staticmethod(_strip_env_prefix)
    _required_node_bins = staticmethod(_required_node_bins)
    _rm_dir = staticmethod(_rm_dir)

    def _inject_npm_cache_env(self, command: str, cwd: str, project_root: str) -> str:
        """给 npm/npx 命令加上可写缓存目录前缀。

        使用与依赖安装阶段相同的解析（命令 NPM_CONFIG_CACHE / --cache、项目
        .npmrc、项目内 .npm-cache、可写 /tmp/npm-cache），使真实命令和安装前步骤
        在缓存上一致。已自带缓存设置——两种大小写的 npm_config_cache=，或
        --cache——的命令原样保留（用户的显式选择优先）。
        Linux 容器支持符号链接，因此 npm 无需关闭 bin 链接即可在
        ``node_modules/.bin`` 创建本地包二进制。
        """
        if not re.match(r"^(npm|npx)\b", self._strip_env_prefix(command)):
            return command
        if re.search(r"npm_config_cache\s*=", command, re.IGNORECASE) or "--cache" in command:
            return command
        if sys.platform != "win32":
            pkg_dir = _find_package_dir(cwd, project_root, command) or Path(cwd)
            cache_dir = _resolve_npm_cache_dir(pkg_dir, command)
            if cache_dir is not None:
                # 与安装阶段相同的引用：该值通过 .npmrc/环境前缀受攻击者影响，
                # 否则会被 shell 按词拆分。
                command = f"npm_config_cache={shlex.quote(cache_dir)} {command}"
        return command

    async def _ensure_node_deps(self, command: str, cwd: str, context: ToolContext) -> str | None:
        """委托给模块级 ensure_node_deps（与 API 的脚本执行器共享）——环境的
        构建方式与命令自身完全相同。"""
        return await ensure_node_deps(command, cwd, context.project_fs_path, _build_env(context))

    async def _ensure_playwright_browsers(self, command: str, cwd: str, context: ToolContext) -> str | None:
        """委托给模块级 ensure_playwright_browsers（与 API 的 spec 运行器共享）；
        TimeoutError 映射为工具的错误形态。"""
        try:
            return await ensure_playwright_browsers(
                command, cwd, context.project_fs_path, _build_env(context)
            )
        except asyncio.TimeoutError:
            return (
                f"Browser preflight timed out after {PLAYWRIGHT_BROWSER_TIMEOUT}s "
                "- retry, or run the spec through the platform server-side channel"
            )
