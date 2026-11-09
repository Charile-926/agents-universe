"""代码执行工具——带 30 秒超时的沙箱 Python/Bash 子进程。

Python 代码在文件访问审计钩子守卫（agent_core.sandbox_guard）下运行，把读写
限制在项目目录内。Bash 代码按与 shell 工具相同的路径规则逐行校验。工作目录和
临时文件都位于项目内（.tmp/work/），因此守卫能覆盖它们；不注入 TEMP/TMP/TMPDIR。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..sandbox import (
    _SAFE_ENV_PATH_VARS,
    _check_all_substitutions,
    _check_assignment,
    _dollar_vars,
    _find_heredoc_start,
    _skip_subst_parens,
    ends_with_continuation,
    has_unclosed_quote,
    python_guard_env,
    spawn_in_new_session,
    split_logical_lines,
    terminate_process_tree,
    validate_command,
)
from ._media import _IMAGE_SUFFIXES, media_type_for, media_url, sanitize_suffix
from .base import Tool, ToolContext
from .shell import redact_secrets

_log = logging.getLogger(__name__)
_TIMEOUT = 30
_PW_TIMEOUT = 120
_MAX_OUTPUT = 20_000

# 校验期间跳过其所在行的 Bash 控制结构关键字。
_BASH_CONTROL = frozenset({
    "if", "then", "else", "elif", "fi", "for", "do", "done",
    "while", "until", "case", "esac", "function", "select", "in",
    "{", "}", "!",
})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Bash 赋值前缀关键字：`export C=$(...)`、`local x=...` 等，与裸 `C=$(...)`
# 一样执行带命令替换的赋值，因此校验器必须在下方 _ASSIGN_RE 匹配前先剥离前缀，
# 否则 `export C=$(echo /etc/passwd)` 会跳过污染分支，`cat $C` 展开为未校验的
# 绝对路径。可选的单字母标志也一并消费（`declare -p`、`readonly -a`）。
_ASSIGN_PREFIX_RE = re.compile(r"^(?:export|local|declare|typeset|readonly)(?:\s+-[A-Za-z]+)*\s+")
# 代理 URL 的协议前缀。用它而非 urlparse 来剥离用户信息，因为无协议的
# "user:pass@host:port" 会被解析成 scheme="user"，凭证会被整个漏掉。
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _proxy_credential_variants(proxy_url: str) -> dict[str, str]:
    """带凭证代理 URL 中绝不能出现在输出里的子串。

    按最宽优先排序，使整个 URL 先于其各部分被替换。不带密码的用户名不视为密钥：
    屏蔽短名称的每次出现会搅乱无关输出。
    """
    if not proxy_url or "@" not in proxy_url:
        return {}
    userinfo = _SCHEME_RE.sub("", proxy_url.rpartition("@")[0], count=1)
    _, sep, password = userinfo.partition(":")
    if not sep or not password:
        return {}
    variants = {
        "PROXY_URL": proxy_url,
        "PROXY_CREDENTIALS": userinfo,
        "PROXY_PASSWORD": password,
    }
    # 百分号编码的拼写会原样进入错误消息，其解码形式会泄漏编码本想隐藏的凭证。
    decoded_userinfo = unquote(userinfo)
    if decoded_userinfo != userinfo:
        variants["PROXY_CREDENTIALS_DECODED"] = decoded_userinfo
    decoded_password = unquote(password)
    if decoded_password != password:
        variants["PROXY_PASSWORD_DECODED"] = decoded_password
    return variants


def redact_proxy_credentials(text: str, proxy_url: str) -> str:
    """从工具可见文本中清洗带凭证的代理 URL。

    打印自身环境的沙箱代码——或回显其无法连接代理的 requests/httpx 错误——否则会
    把凭证交给模型和服务器日志。在解码之后、截断之前应用；精确子串替换，同
    shell.redact_secrets。
    """
    return redact_secrets(text, _proxy_credential_variants(proxy_url))


def _live_payloads(text: str, *, quotes_literal: bool) -> list[str]:
    r"""bash 会在 *text* 内执行的替换载荷。

    ``quotes_literal=False`` 是 heredoc 正文上下文：那里引号是普通字符，因此其中的
    `$(...)` 仍会执行。``quotes_literal=True`` 是 shell 解析上下文（载荷自身的
    内容）：单引号区域绝不执行并被跳过，双引号则会执行。递归进入每个载荷很重要，
    因为外层 shlex 处理会保留引号内的反斜杠转义——否则 `"$(c\url x)"` 会让
    单级扫描漏掉 `curl`。
    """
    payloads: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if quotes_literal and c == "'":
            end = text.find("'", i + 1)
            if end == -1:
                break  # 未闭合——bash 语法错误，没有东西执行
            i = end + 1
            continue
        if c == "`":
            end = text.find("`", i + 1)
            if end == -1:
                break  # 未闭合——_validate_substitutions 先失败关闭
            payload = text[i + 1 : end]
            payloads.append(payload)
            payloads.extend(_live_payloads(payload, quotes_literal=True))
            i = end + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            close = _skip_subst_parens(text, i + 2)
            if close is None:
                break  # 未闭合——被 _validate_substitutions 拒绝
            payload = text[i + 2 : close]
            payloads.append(payload)
            payloads.extend(_live_payloads(payload, quotes_literal=True))
            i = close + 1
            continue
        i += 1
    return payloads


class CodeExecutorTool(Tool):
    name = "code_executor"
    prompt_hint = (
        "For computation or data processing that needs real code. File access confined "
        "to the project workspace; network follows the SANDBOX_NETWORK policy "
        "(default: allowed), 30s timeout. Use language=python-playwright for browser "
        "automation (Playwright Python): subprocess + network allowed so the agent can "
        "drive a real browser - downloads, screen recording, screenshots, etc."
    )
    description = (
        "Execute Python, Python-Playwright, or Bash code in a sandboxed subprocess. "
        "Python/Bash: 30s timeout. Network follows the SANDBOX_NETWORK policy "
        "(default: allowed; 'localhost' or 'none' restrict it). "
        "python-playwright: 120s timeout, subprocess + network allowed for browser "
        "automation via the Playwright Python library. "
        "Python code runs under a file-access guard that restricts reads/writes to "
        "the project directory; cross-project access via ../ or absolute paths is blocked. "
        "The working directory is the project root, available as the PROJECT_DIR env var. "
        "Files written to the OUTPUT_DIR env var are delivered to the user "
        "(images appear in chat, other files as downloadable attachments) — "
        "write anything the user should receive there "
        "(e.g., $OUTPUT_DIR/output_0.png, $OUTPUT_DIR/report.csv). "
        "Outbound HTTP(S) follows the platform proxy — the sandbox environment "
        "carries the resolved URL as HTTPS_PROXY; a browser launched with Playwright "
        "must be given it explicitly as proxy=os.environ['HTTPS_PROXY'] because "
        "Playwright ignores proxy env vars. "
        "Bash mode is for in-project data processing; use the shell tool for external commands."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Code to execute"},
            "language": {
                "type": "string",
                "enum": ["python", "bash", "python-playwright"],
                "default": "python",
            },
        },
        "required": ["code"],
    }

    # ctypes 在文本黑名单上（不在运行时导入黑名单——pandas 内部会导入它）；显式
    # 点名它的用户代码被拒绝，而库的导入继续可用。
    _BLOCKED_IMPORTS = re.compile(
        r"\b(subprocess|os\.system|os\.popen|os\.execv|os\.execl|os\.fork|pty|ctypes|_ctypes|pexpect|fabric|invoke)\b"
    )
    # Playwright 模式允许 subprocess（启动浏览器），但仍拦截直接的
    # os.system/fork/pty/ctypes 等。审计钩子无论如何都会拦截运行时调用；该正则是
    # 文本级的第一道门。
    _BLOCKED_IMPORTS_PW = re.compile(
        r"\b(os\.system|os\.popen|os\.execv|os\.execl|os\.fork|pty|ctypes|_ctypes|pexpect|fabric|invoke)\b"
    )
    _BLOCKED_BASH = re.compile(
        # 解释器能绕过仅针对 Python 的审计钩子（perl -e 运行任意代码），git 经
        # GIT_SSH_COMMAND / 凭证助手 / 代理访问网络——该正则也把关 scripts.py。
        # 数据库客户端（mysql/psql/redis-cli/mongosh/...）能访问存放会话/密钥数据的
        # compose 内部数据库；bash 模式没有套接字审计钩子，因此像 curl/wget 一样
        # 在此拦截。
        r"\b(curl|wget|npx|npm|node|tsx|python3?|ssh|scp|rsync|nc|ncat|telnet|ftp|"
        r"mysql|mariadb|psql|redis-cli|redis-server|mongosh|mongo|"
        r"clickhouse-client|tsql|"
        # shell/元编程词在读取主机文件前先运行任意代码——`eval "$(echo cat
        # /etc/hostname)"`、`echo 'cat /etc/passwd' > x.sh; . x.sh`、`bash x.sh`
        # 都在真实执行载荷时绕过令牌检查。trap/alias/shopt 是具备同等能力的 shell
        # 内建命令（trap 'cat /etc/passwd' EXIT 在脚本结束时运行；alias 加
        # shopt -s expand_aliases 在非交互 bash 中运行别名命令）；xargs/dd 把
        # 命令/路径构造偷渡进令牌检查看不到的数据令牌（`echo /etc/passwd | xargs
        # cat`、`dd if=/etc/passwd`）。
        r"bash|sh|zsh|dash|ksh|fish|ash|eval|source|exec|command|builtin|env|"
        r"perl|ruby|php|git|socat|openssl|unset|trap|alias|shopt|xargs|dd)\b|"
        # ANSI-C 引用（$'\x2fetc\x2fpasswd'）把 '/' 和 '..' 藏在 shlex 从不反转义的
        # 令牌里——bash 在运行时把它解码成绝对路径，越过所有检查。
        r"\$\'|"
        # 点命令（`. script.sh`）不是 \b 词——只在开头/分隔符之后匹配，使
        # `./tool.sh` 继续可用。
        r"(?:^|[\s;|&])\.(?=\s|$)"
    )

    @classmethod
    def _payload_blocked_word(cls, text: str, *, quotes_literal: bool) -> str | None:
        r"""*text* 的活替换载荷内的封禁词，没有则返回 None。

        行级 shlex 保留引号内的反斜杠转义，因此 `"$(c\url x)"` 让整行搜索漏掉
        `curl`，尽管替换会执行它——单独遍历载荷即可看到未加引号的内部文本。载荷无法
        分词时抛 ValueError（失败关闭）。
        """
        for payload in _live_payloads(text, quotes_literal=quotes_literal):
            lexer = shlex.shlex(payload, posix=True)
            lexer.whitespace_split = True
            unescaped = " ".join(list(lexer))
            match = cls._BLOCKED_BASH.search(unescaped)
            if match:
                return match.group(0)
        return None

    @classmethod
    def _find_blocked_bash(cls, code: str) -> str | None:
        r"""bash 脚本包含的封禁词，没有则返回 None。

        按逻辑行扫描并跳过 heredoc 正文：整段代码的 shlex 会因 heredoc 正文里的
        撇号报错，原始文本回退随后漏掉后一行上反斜杠混淆的命令（`c\url
        http://...`），bash 会把它反转义回 `curl` 并真实执行。正文行是 stdin 数据
        ——其活替换载荷改由 _validate_bash 检查，它知道定界符是否被引用。某行无法
        分词时抛 ValueError（失败关闭）。与脚本运行器共享。
        """
        for logical, is_heredoc_body in split_logical_lines(code):
            if is_heredoc_body:
                continue
            lexer = shlex.shlex(logical, posix=True)
            lexer.whitespace_split = True
            unescaped = " ".join(list(lexer))
            match = cls._BLOCKED_BASH.search(unescaped)
            if match:
                return match.group(0)
            match = cls._payload_blocked_word(logical, quotes_literal=True)
            if match:
                return match
        return None

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        code = params["code"]
        language = params.get("language", "python")

        # JSON schema 的 enum 只是建议性的——手工构造的工具调用可传任何值。下方每个
        # 守卫都以精确字符串为键，因此未知值（"sh"、"shell"、"Bash"）会跳过它们，
        # 全部落到 bash 分支：没有黑名单、没有路径校验的任意 shell。改为拒绝未知值。
        if language not in ("python", "python-playwright", "bash"):
            return {"error": f"Unsupported language {language!r}. Use python, python-playwright or bash."}

        if language == "python" and self._BLOCKED_IMPORTS.search(code):
            return {"error": "Code uses a blocked module (subprocess, os.system, etc.). Use the shell tool for commands."}
        if language == "python-playwright" and self._BLOCKED_IMPORTS_PW.search(code):
            return {"error": "Code uses a blocked module (os.system, pty, ctypes, etc.). Use the shell tool for commands."}
        if language == "bash":
            # ANSI-C 引用（$'\x2fetc\x2fpasswd'）把 '/' 和 '..' 藏在 bash 运行时才
            # 解码的令牌里——下方 shlex 反转义会剥离引号，使 $'-模式永远匹配不上，
            # 令牌检查只看到解码内容。检查原始代码；数据 echo 里字面 `$'` 文本是罕见
            # 误报，失败关闭。
            if "$'" in code:
                return {"error": "Bash code_executor is for data processing only. ANSI-C quoting ($'...') is not allowed."}
            # \b-word 正则跑在原始代码上可被反斜杠混淆绕过（`c\url ...` 中
            # "curl" 不是连续子串，但 bash 执行时反斜杠转义还原出真正的
            # `curl`）。先按 shlex 还原转义再匹配，堵住该旁路。
            # 无法分词的行会被拒绝（逐行/heredoc 正文的理由见
            # _find_blocked_bash）。
            try:
                blocked = self._find_blocked_bash(code)
            except ValueError:
                return {"error": "Bash code could not be parsed safely; refusing to run."}
            if blocked:
                return {"error": "Bash code_executor is for data processing only. Use the shell tool for external commands."}

        project_cwd = Path(context.project_fs_path)

        # 启动前校验 bash 代码是否存在路径越界。这里允许命令替换（与 shell 工具不同），
        # 因为它在数据处理片段中很常见，且 _BLOCKED_BASH 已剥离可能滥用它的外部
        # 命令。
        if language == "bash":
            reason = self._validate_bash(code, project_cwd)
            if reason:
                return {"error": reason}

        # 项目内的临时目录（.tmp/work/），使 Python 文件访问守卫能覆盖脚本文件和
        # 图片输出。在 finally 中清理。不注入 TEMP/TMP/TMPDIR——用户明确要求临时目录
        # 留在项目内。
        work_dir = project_cwd / ".tmp" / "work" / f"code-{uuid.uuid4().hex[:8]}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            output_dir = work_dir / "output"
            output_dir.mkdir(exist_ok=True)

            if language in ("python", "python-playwright"):
                code_file = work_dir / "script.py"
                # Windows 上用 newline="\n"：文本模式 write_text 会把 \n 转成
                # \r\n，而在 Linux（或 Git Bash/WSL）回读的 bash 脚本会因命令名和
                # 文件路径里的尾随 \r 出错。
                code_file.write_text(code, encoding="utf-8", newline="\n")
                cmd = [sys.executable, str(code_file)]
            else:  # bash——上面守卫之后唯一剩下的值
                # Bash 从脚本“文件”运行（而非经 stdin 的 `bash -s`），使脚本自身的
                # stdin 保持空闲：裸 `cat`/`read` 得到 EOF，而不是吞掉剩余脚本行。
                # 路径相对 cwd（项目根），在 Linux bash、Git Bash 和 WSL 中行为一致
                # ——无需盘符映射。
                code_file = work_dir / "script.sh"
                code_file.write_text(code, encoding="utf-8", newline="\n")
                cmd = ["bash", code_file.relative_to(project_cwd).as_posix()]

            # safe_env() 剥离 SECRET_KEY、数据库连接字符串等。
            # python_guard_env 在子进程中武装 sitecustomize 审计钩子（strict=True
            # 拒绝 subprocess.Popen 调用，作为第二层与上面的 _BLOCKED_PYTHON 正则
            # 对应；subprocess 导入仍允许，使 matplotlib/pandas 继续工作）。
            extra: dict[str, str] = {
                "OUTPUT_DIR": str(output_dir),
                "PROJECT_DIR": str(project_cwd),
            }
            # 网络策略遵循 SANDBOX_NETWORK 配置（见 .env.example）：未设置/空——
            # 允许所有网络（Playwright 下载、屏幕录制……）；localhost——仅环回；
            # none——阻断所有套接字。只有限制模式需要向下传递；"all" 是
            # sitecustomize 默认。
            network_mode = (context.cfg("SANDBOX_NETWORK") or "").strip().lower()
            if language == "python":
                extra["HOME"] = str(work_dir)
                extra.update(python_guard_env(project_cwd, strict=True))
                if network_mode in ("localhost", "none"):
                    extra["_AGENT_NETWORK_MODE"] = network_mode
            elif language == "python-playwright":
                # 保留真实 HOME，使 Playwright 能找到浏览器二进制（PLAYWRIGHT_BROWSERS_PATH
                # 从 Docker ENV 继承）。非严格：允许 subprocess.Popen 以启动浏览器。
                # 网络遵循 SANDBOX_NETWORK（默认：允许所有）。
                extra.update(python_guard_env(project_cwd, strict=False))
                if network_mode in ("localhost", "none"):
                    extra["_AGENT_NETWORK_MODE"] = network_mode
            else:  # bash
                # 继承真实 HOME 会把主机的点文件（.aws/.azure/.ssh）交给沙箱，而
                # bash 的 $HOME 在项目内路径白名单（_SAFE_ENV_PATH_VARS）上——该
                # 白名单只有在 HOME 确实指向项目内时才可靠。像 python 一样把它指向
                # 临时目录。
                extra["HOME"] = str(work_dir)

            # 代理环境在 safe_env() 之后处理，因为合并只能设置值，无法删除空的
            # HTTPS_PROXY 占位符或驱逐冲突的继承拼写。每个网络模式都拿到与浏览器工具
            # 相同的代理——由 SANDBOX_NETWORK 决定守卫允许哪些套接字，而非进程配置
            # 哪个代理。代码自己启动的 Chromium 不读这些变量（Playwright 只认显式的
            # launch(proxy=...) 参数），但 requests/httpx/urllib 和子进程会读。
            proxy_url = context.proxy_url()
            env = context.proxy_env(context.safe_env(extra=extra))

            # 在 await 之前绑定：否则在 create_subprocess_exec 仍在等待时送达的取消，
            # 会带着未绑定的 `proc` 命中 except 处理，用 UnboundLocalError 取代
            # CancelledError（运行随后无法干净地取消）。
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    # stdin 用 DEVNULL：读 stdin 的脚本得到干净 EOF，符合“运行脚本
                    # 文件”的语义。
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(project_cwd),
                    env=env,
                    # POSIX 上用独立会话，使分叉/后台的孙进程在超时/中止时随进程树
                    # 一起终止。
                    **spawn_in_new_session(),
                )
                timeout = _PW_TIMEOUT if language == "python-playwright" else _TIMEOUT
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                if proc is not None:
                    terminate_process_tree(proc)
                    await proc.wait()
                _log.warning("code_executor timed out after %ds (language=%s)", timeout, language)
                return {"error": f"Execution timed out after {timeout}s", "exit_code": -1}
            except asyncio.CancelledError:
                # 任务取消：在子进程 finally 里 rmtree work_dir 之前先杀掉它（存活
                # 子进程会让脚本文件保持打开）。
                if proc is not None:
                    terminate_process_tree(proc)
                    await proc.wait()
                raise
            except OSError as exc:
                _log.warning("code_executor failed to start subprocess: %s", exc)
                return {"error": f"Failed to start code execution: {exc}"}

            # 在截断之前脱敏，使跨越 _MAX_OUTPUT 边界的代理凭证被完整屏蔽；下方日志
            # 行读取同一清洗后的文本。
            stdout_text = redact_proxy_credentials(
                stdout.decode(errors="replace"), proxy_url)[:_MAX_OUTPUT]
            stderr_text = redact_proxy_credentials(
                stderr.decode(errors="replace"), proxy_url)[:_MAX_OUTPUT]
            if proc.returncode != 0:
                _log.warning("code_executor failed (exit=%d, language=%s)\nstderr: %s",
                             proc.returncode, language, stderr_text[:500])

            # 从隔离的输出目录收集产物：PNG 以图片呈现，其他一切作为可下载附件。
            # 名称由服务器生成（媒体白名单拒绝空格和 unicode——沙箱代码可写任意
            # 文件名），原始名放在记录的 `name` 字段里。
            images: list[dict[str, str]] = []
            files: list[dict[str, Any]] = []
            media_path = Path(context.conversation_media_dir)
            media_path.mkdir(parents=True, exist_ok=True)
            for out_file in sorted(output_dir.iterdir()):
                if not out_file.is_file():
                    continue
                suffix = sanitize_suffix(out_file.name)
                dest = media_path / f"code_{uuid.uuid4().hex[:8]}{suffix}"
                try:
                    shutil.move(str(out_file), str(dest))
                except OSError:
                    continue
                rel = media_url(context, dest.name)
                if suffix in _IMAGE_SUFFIXES:
                    images.append({"id": dest.stem, "url": rel, "alt": f"Code output: {out_file.name}", "path": str(dest)})
                else:
                    files.append({
                        "id": dest.stem,
                        "url": rel,
                        "name": out_file.name[:255],
                        "media_type": media_type_for(out_file.name),
                        "size": dest.stat().st_size,
                    })

            result: dict[str, Any] = {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": proc.returncode,
            }
            if images:
                result["images"] = images
            if files:
                result["files"] = files
            return result
        except OSError as exc:
            return {"error": f"Failed to create scratch directory inside the project: {exc}"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Bash 校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_bash(code: str, project_root: Path) -> str | None:
        """对 bash 脚本做尽力而为的逐行路径越界检查。

        跳过注释、控制结构关键字、变量赋值和 heredoc 正文。引号未闭合的物理行先与
        后续行拼接——带引号字符串可合法跨多行。允许命令替换（_BLOCKED_BASH 正则
        已剥离可能滥用它的外部命令）；剩余路径令牌由 validate_command 检查。
        """
        in_heredoc = False
        heredoc_delim: str | None = None
        heredoc_quoted = False
        buffer = ""
        buffer_start = 0
        # 值从命令替换捕获的变量名（`x=$(echo /etc/passwd)`）。之后在路径位置使用
        # 它们时，运行时会展开为任意内容——validate_command 拒绝这些引用。
        tainted: set[str] = set()
        # 本脚本更早行上赋值的变量名。注入每次逐行 validate_command，使第 1 行的
        # `d=data` 仍能在第 3 行满足已赋值路径前缀规则（`cat $d/x`）；每个值在其
        # 赋值行通过时都已校验（_check_simple 拒绝 `..` 和绝对路径），因此向前
        # 携带该名安全。
        assigned: set[str] = set()
        lines = code.splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            line_num = idx + 1
            idx += 1
            if in_heredoc:
                if line.strip() == heredoc_delim:
                    in_heredoc = False
                    heredoc_delim = None
                    heredoc_quoted = False
                    continue
                # 未加引号的 heredoc 正文在 bash 执行时会运行命令替换——`cat <<EOF`
                # 加正文里的 `$(cat /app/.env)` 之前因正文行被整体跳过而蒙混过关。
                # 引用定界符（<<'EOF'）是字面量，跳过安全。
                if not heredoc_quoted:
                    # 单引号在 heredoc 正文里是普通字符——其中的 $(...) 仍执行，
                    # 因此要扫描。bash 也会在未加引号正文里拼接反斜杠-换行（且续接
                    # 目标不做定界符检查），所以扫描前先合并续接行：`$(cat \` 加
                    # `/etc/passwd)` 行必须作为一个替换整体检查。
                    body = line
                    while ends_with_continuation(body) and idx < len(lines):
                        body = body[:-1] + lines[idx]
                        idx += 1
                    reason = CodeExecutorTool._validate_substitutions(
                        body, buffer_start, project_root, single_quotes_literal=False
                    )
                    if reason:
                        return reason
                    # 正文行被 _find_blocked_bash 跳过（它们是 stdin 数据），但未加
                    # 引号正文的 $(...) / 反引号会真实执行——bash 模式没有套接字审计
                    # 钩子，因此埋在这里的 `$(curl ...)` 或 `$(python ...)` 会越过
                    # 黑名单访问网络/未守卫的解释器。只扫描载荷：周围的数据词仍不是
                    # 命令。
                    reason = CodeExecutorTool._blocked_substitutions(body)
                    if reason:
                        return reason
                continue
            candidate = f"{buffer}\n{line}" if buffer else line
            if not buffer:
                buffer_start = line_num
            stripped = candidate.strip()
            if not stripped or stripped.startswith("#"):
                buffer = ""
                continue
            if has_unclosed_quote(candidate):
                # 多行带引号字符串——先累积，在校验前与下一物理行重试。
                buffer = candidate
                continue
            buffer = ""
            # 检测 heredoc 开始，以便后续迭代跳过正文行。整行正则搜索会匹配引号
            # “内”的 "<<EOF"（`echo "x <<EOF"`），在 bash 真实运行后续行时把它们
            # 置于 heredoc 跳过模式——任意命令逃过校验。_find_heredoc_start 用引号
            # 跟踪扫描原始行，同时捕获定界符的引用标志：引用定界符（<<'EOF'）使
            # 正文成为字面量，因此其 $(...) 不得当作可执行。
            heredoc = _find_heredoc_start(stripped)
            if heredoc is not None:
                heredoc_delim, heredoc_quoted = heredoc
                in_heredoc = True
            first_word = stripped.split()[0].rstrip(";")
            if first_word in _BASH_CONTROL:
                # 控制结构行过去被整体跳过校验——但其正文会真实执行：`for i in
                # $(cat ../sibling/.env)` 和 `do cat ../x; done` 都绕过了路径检查
                # （控制关键字本身作为普通项目内令牌校验，因此整行检查安全）。
                reason = validate_command(
                    stripped,
                    cwd=project_root,
                    project_root=project_root,
                    allow_substitution=True,
                    tainted_vars=frozenset(tainted),
                    pre_assigned_vars=assigned,
                )
                if reason:
                    return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                continue
            # `export C=$(...)` / `local x=...` 也是赋值——剥离前缀，使
            # `export C=$(echo /etc/passwd)` 无法跳过污染分支（否则其 `$C` 会越过
            # 校验展开）。
            assign_body = _ASSIGN_PREFIX_RE.sub("", stripped) if _ASSIGN_PREFIX_RE.match(stripped) else stripped
            if _ASSIGN_RE.match(assign_body):
                # 赋值行被整体跳过，但 `X=$(cat /app/.env)` 执行命令替换——载荷必须
                # 像任何其他命令一样校验。该行其余部分仍运行（`x=$(echo /etc/passwd);
                # cat $x` 把捕获值放进路径参数），因此整行经 validate_command，
                # 替换捕获的名作为污染跟踪。
                reason = CodeExecutorTool._validate_substitutions(stripped, buffer_start, project_root)
                if reason:
                    return reason
                try:
                    # 引号感知令牌：`a="x y" b=$t` 必须产生两个赋值——普通空白
                    # 拆分行会在引号内空格处切断，在 `y"` 处退出循环，让 `b` 未被
                    # 跟踪，使 `cat $b` 溜过。
                    assign_tokens = shlex.split(stripped)
                except ValueError:
                    # 未闭合引号——该行是 bash 语法错误，validate_command 会在下方
                    # 拒绝；没有东西需要污染。
                    assign_tokens = []
                for tok in assign_tokens:
                    if not _ASSIGN_RE.match(tok):
                        # 不是赋值——被 shlex 粘到 `x=$(echo hi)` 上的载荷词
                        # （`hi)`）或命令令牌；继续扫描，使同行稍后的赋值仍被跟踪
                        # （`x=$(echo hi) C=$(echo /etc/passwd)` 必须污染 C）。
                        continue
                    # `export x=/etc/passwd`——当令牌作为普通位置参数校验时，NAME=
                    # 前缀会消解绝对路径扫描（前缀是整个令牌的开头段）。值必须通过
                    # 赋值校验：绝对/波浪号/`..` 路径和未跟踪变量引用被拒绝，且只有
                    # 值干净时该名才获得“已赋值”状态。
                    reason = _check_assignment(tok, allow_dollar=True,
                                               assigned_vars=assigned)
                    if reason:
                        return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                    name, _, value = tok.partition("=")
                    # shlex（无 punctuation_chars）会把命令分隔符粘到令牌上
                    # （`x=;`、`x=a&&b`）——bash 把它解析为边界，因此 `x=;` 是空
                    # 赋值，不得授予“已赋值”状态。
                    value = value.rstrip(";&|()")
                    refs = set(_dollar_vars(value))
                    # 展开污染变量的变量（`y=$x`）自身也污染——`y=$x; cat $y` 像
                    # `x=$(echo /etc/passwd); cat $x` 一样通过中继偷渡捕获路径。对
                    # 未跟踪名的引用也污染：主机环境变量（$SHELL）展开为主机路径，
                    # 未设置变量展开为空，使 `$x/etc/hostname` 坍缩成绝对路径。只有
                    # 本屏/本行更早赋值的名和服务器注入的项目内变量可预测。
                    untracked = refs - assigned - _SAFE_ENV_PATH_VARS
                    if "$(" in value or "`" in value or (refs & tainted) or untracked:
                        tainted.add(name)
                    # 只有非空值才算“已赋值”——`x=` 展开为空，因此
                    # `cat $x/etc/hostname` 会越过检查坍缩成绝对路径。
                    # （validate_command 独立拒绝值中的 ${x:-...} 展开。）
                    if value:
                        assigned.add(name)
                reason = validate_command(
                    stripped,
                    cwd=project_root,
                    project_root=project_root,
                    allow_substitution=True,
                    tainted_vars=frozenset(tainted),
                    pre_assigned_vars=assigned,
                )
                if reason:
                    return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                continue
            reason = validate_command(
                stripped,
                cwd=project_root,
                project_root=project_root,
                allow_substitution=True,
                tainted_vars=frozenset(tainted),
                pre_assigned_vars=assigned,
            )
            if reason:
                return f"Line {buffer_start}: {reason}\n  -> {stripped}"
        if buffer:
            return f"Line {buffer_start}: command could not be parsed safely (unclosed quotation)\n  -> {buffer.strip()}"
        return None

    @staticmethod
    def _validate_substitutions(
        line: str, line_num: int, project_root: Path, *, single_quotes_literal: bool = True
    ) -> str | None:
        """校验本会被 _validate_bash 跳过的行中的命令替换载荷（未引用 heredoc
        正文、赋值值）。

        `X=$(cat /app/.env)` 和带 `$(...)` 的 heredoc 正文在 bash 运行该行时执行
        载荷；载荷必须通过与任何其他命令相同的路径范围校验。`$(< file)`（纯文件
        读取）被直接拒绝。_check_all_substitutions 遍历每个嵌套层——
        `$(cat $(echo /etc/passwd))` 只有外层被校验时才会被抓到（内层是无害的数据
        命令）。未引用 heredoc 正文里引号对 bash 是普通字符（替换仍运行），因此
        single_quotes_literal=False。
        """
        reason = _check_all_substitutions(
            line,
            cwd=project_root,
            root=project_root,
            single_quotes_literal=single_quotes_literal,
        )
        if reason:
            return f"Line {line_num}: command substitution blocked: {reason}"
        return None

    @staticmethod
    def _blocked_substitutions(text: str) -> str | None:
        """拒绝活替换载荷内的封禁命令。

        补充跳过 heredoc 正文的 _find_blocked_bash：只有未引用正文的载荷会执行
        （引用正文是字面量，从不到这里），因此只有它们需要命令词检查。正文上下文
        让引号保持字面——未引用正文里普通单引号中的 `$(...)` 仍运行。
        """
        try:
            match = CodeExecutorTool._payload_blocked_word(text, quotes_literal=False)
        except ValueError:
            return "Bash code could not be parsed safely; refusing to run."
        if match:
            return (
                "Bash code_executor is for data processing only. "
                "Use the shell tool for external commands."
            )
        return None
