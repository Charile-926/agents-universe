"""沙箱工具：Python 子进程守卫环境 + 共享的 shell 命令校验器。

供 shell 工具（PR-2）以及后续 code_executor 的 bash 模式（PR-3）使用。路径
约束本身位于 agent_core.paths（PR-1）。本模块负责为子进程装配审计钩子环境、
按进程组清理子进程树，以及校验 shell 命令（命令注入拦截、危险命令黑名单、
路径越界检查等）。
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import tempfile
from pathlib import Path

from .paths import is_within

GUARD_DIR = Path(__file__).parent / "sandbox_guard"

_ROOT_ENV = "_AGENT_PROJECT_ROOT"
_BLOCK_SUBPROCESS_ENV = "_AGENT_BLOCK_SUBPROCESS"
_EXEC_ALLOWLIST_ENV = "_AGENT_EXEC_ALLOWLIST"

# 受守卫（非严格）Python 允许 os.exec 进入的二进制目录。
# _ALLOWED_ABSOLUTE_COMMANDS 中的 semgrep 控制台脚本，最后会 execvp 其 venv
# 里的原生 osemgrep 二进制（替换自身进程）；否则 sitecustomize 审计钩子会
# 拒绝该 os.exec 事件并在扫描中途终止——Dockerfile 构建期的冒烟检查捕获不到
# 这一点，因为它运行时没有守卫环境。不引入新的逃逸面：非严格模式本就允许
# subprocess.Popen 这些相同二进制，且被 exec 的进程保留 shell 工具的会话，
# 超时杀进程仍能到达它。严格模式（code_executor）从不设置该环境变量，钩子
# 在那里也会忽略它。
_EXEC_ALLOW_DIRS = ("/opt/semgrep-venv",)


def spawn_in_new_session(*, posix_only: bool = True) -> dict[str, bool]:
    """create_subprocess_exec 的 kwargs 辅助函数：把子进程放进独立会话，
    使 terminate_process_tree() 能触及每个后代。
    Windows 的 asyncio 子进程不支持 start_new_session。"""
    if posix_only and os.name != "posix":
        return {}
    return {"start_new_session": True}


def terminate_process_tree(proc) -> None:
    """杀掉整个进程树，而不只是直接子进程。

    否则 fork/后台运行的孙进程（``sleep 300 &``、``sh -c '... &'``）会在
    proc.kill() 后存活，在工具返回或超时后继续运行——甚至继续写入。子进程
    必须用 spawn_in_new_session() 启动，killpg 才能触及每个后代；Windows
    （没有进程组）退化为普通 kill()。
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # 已不存在
    else:
        proc.kill()


def _guard_pythonpath(project_root: str | Path, inherited: str) -> str:
    """先放 GUARD_DIR，再放继承来的、仍位于项目内的 PYTHONPATH 条目。

    API 服务器把 PYTHONPATH=/app/src（框架源码，位于每个项目工作区之外）继承
    进每个工具派生的 Python。保留该条目比删掉更糟：导入会静默跳过它（守卫的
    PermissionError 被导入机制吞掉），转而解析到已安装的某个 wheel——分支检出
    的测试会对着陈旧的 site-packages 代码运行——而 pip 之类工具在扫描
    sys.path 时会触发守卫。保留相对条目（子进程的 cwd 始终在项目内），也保留
    项目内的绝对条目；其他一律丢弃。
    """
    parts = [str(GUARD_DIR)]
    if not inherited:
        return os.pathsep.join(parts)
    root = Path(project_root).resolve()
    for entry in inherited.split(os.pathsep):
        if not entry:
            parts.append(entry)  # 内部空条目 == cwd（在项目内）
            continue
        candidate = Path(entry)
        # 即使在 Windows 上（Path.is_absolute() 只识别盘符），也把开头的
        # '/'（或 '\'）视为绝对路径。
        if not candidate.is_absolute() and not entry.startswith(("/", "\\")):
            candidate = root / candidate  # 在项目内解析相对条目
        try:
            if is_within(root, candidate):
                parts.append(entry)
        except OSError:
            continue  # 无法解析的条目——失败时关闭（fail closed）
    return os.pathsep.join(parts)


def python_guard_env(project_root: str | Path, *, strict: bool = False) -> dict[str, str]:
    """为派生的 Python 子进程装配 sitecustomize 守卫所需的环境变量。

    把 GUARD_DIR 前置到 PYTHONPATH 并设置 ``_AGENT_PROJECT_ROOT``；子解释器
    随后自动导入 ``sandbox_guard/sitecustomize.py``，由它安装审计钩子。命令
    派生的任何嵌套 Python 都会继承该环境，因此守卫会向下传播。strict=True 还
    会阻止 ``import subprocess``（供 code_executor 使用；shell 工具保持非严格，
    让需要 subprocess 的 pytest 插件继续工作），并随之停用下方的 os.exec 允许
    名单。项目外继承来的 PYTHONPATH 条目会被丢弃（见 ``_guard_pythonpath``）。
    TEMP/TMP/TMPDIR 刻意保持不动。
    """
    env = {_ROOT_ENV: str(Path(project_root).resolve())}
    env["PYTHONPATH"] = _guard_pythonpath(project_root, os.environ.get("PYTHONPATH", ""))
    if strict:
        env[_BLOCK_SUBPROCESS_ENV] = "1"
    else:
        # 为 semgrep venv 二进制开放 os.exec（见 _EXEC_ALLOW_DIRS）。
        env[_EXEC_ALLOWLIST_ENV] = os.pathsep.join(_EXEC_ALLOW_DIRS)
    return env


# ---------------------------------------------------------------------------
# 共享命令校验器
# ---------------------------------------------------------------------------

# 在一条命令字符串内分隔命令的 shlex 标点 token。
_SEPARATORS = frozenset({"&&", "||", ";;", ";", "|", "&", "(", ")"})

# 其后 token 是必须校验的文件路径的重定向操作。
# 带非数字单词的 ">&" 会把 stdout 和 stderr 都重定向到该文件（`cat >& ../x`），
# "<>" 则以读写方式打开文件——两者都是 bash 侧的打开动作，Python 审计钩子看
# 不到，因此它们的目标是路径而非文件描述符编号。（`2>&1` 仍安全：其中的 "1"
# 只是作为项目内单字符名称做路径检查。）
_REDIRECT_PATH_OPS = frozenset({">", ">>", ">|", "&>", "&>>", "<", ">&", "<>"})
# 其后 token 不是路径的重定向（heredoc 分隔符、文件描述符编号）。
_REDIRECT_SKIP_OPS = frozenset({"<<", "<<<", "<&"})

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_PY_RE = re.compile(r"^python3?(?:\.\d+)?$")
_SEP_RE = re.compile(r"[\\/]")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 由 shell 工具允许名单显式放行的绝对路径命令。
# semgrep 在导入时硬性要求 mcp 1.29.0（与同一 site-packages 中 agent-core 的
# mcp>=2.0.0 不兼容），因此它安装在独立 venv 中，并通过其控制台脚本调用。
# 只有命令 token 豁免绝对路径规则；其参数仍要通过所有路径检查。
_ALLOWED_ABSOLUTE_COMMANDS = frozenset({"/opt/semgrep-venv/bin/semgrep"})
def _skip_subst_parens(text: str, start: int) -> int | None:
    """返回 *start* 处（指向 `$(` 之后）$(...) 区域的闭合 `)` 索引。
    嵌套括号按深度计数；引号字符串内的 `)` 不会闭合该区域。永不闭合时返回
    None。"""
    depth = 1
    n = len(text)
    j = start
    while j < n:
        c = text[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c in ("'", '"'):
            q = c
            j += 1
            while j < n:
                if q == '"' and text[j] == "\\" and j + 1 < n and text[j + 1] in ('"', "\\"):
                    j += 2
                    continue
                if text[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None



# 算术扩展区域（$((...))、$[...]，以及开头的 ((...))）会把 << 用作位移
# 运算符——扫描器必须跳过它们，否则 `echo $((1 << 2))` 会打开一个虚假的
# heredoc，其"正文"会把后续行不经校验地吞掉。括号在算术内部嵌套，因此区域
# 在第一个使深度归零的 `))` 处闭合。
def _skip_arith_parens(line: str, start: int) -> int | None:
    depth = 2
    n = len(line)
    j = start
    while j < n:
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _at_command_start(line: str, i: int) -> bool:
    """当 *i* 开启一条命令时为真：行首、位于 ; & | ( { 之后，或位于控制
    关键字（if/while/...）之后。"""
    j = i - 1
    while j >= 0 and line[j] in " \t":
        j -= 1
    if j < 0 or line[j] in ";&|({!":
        return True
    # `if (( 1 << 2 )); then`——算术部分跟在控制关键字之后，而非分隔符之后。
    # 任何与 `((` 粘连的其他单词都是 bash 语法错误，因此关键字白名单只会收窄
    # 跳过范围。
    if line[j].isalnum() or line[j] == "_":
        start = j
        while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
            start -= 1
        return line[start : j + 1] in _CMD_KEYWORDS
    return False


# 会引出一条命令的控制关键字（见 _at_command_start）。
_CMD_KEYWORDS = frozenset({"if", "elif", "then", "else", "while", "until", "for", "do"})


def _skip_braced(line: str, start: int) -> int | None:
    """返回 *start* 处（指向 `${` 之后）打开的 ${...} 区域的闭合 `}` 索引。
    嵌套花括号（`${x:-${y}}`）按深度计数；引号字符串内的 `}` 不会闭合该区域。
    区域内的 `$(...)`、`$((...))` 和反引号整体跳过——bash 的参数解析器不把
    它们内部的 `}` 当作闭合花括号（`${x:-$(echo a})}` 在最后一个 `}` 处闭合）。
    永不闭合时返回 None——该行是 bash 语法错误，且不会打开 heredoc。
    """
    depth = 1
    n = len(line)
    j = start
    while j < n:
        c = line[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c == "$" and j + 1 < n:
            if j + 2 < n and line[j + 1] == "(" and line[j + 2] == "(":
                end = _skip_arith_parens(line, j + 3)
                if end is None:
                    return None
                j = end + 1
                continue
            if line[j + 1] == "(":
                end = _skip_subst_parens(line, j + 2)
                if end is None:
                    return None
                j = end + 1
                continue
        if c == "`":
            end = line.find("`", j + 1)
            if end == -1:
                return None
            j = end + 1
            continue
        if c in ("'", '"'):
            q = c
            j += 1
            while j < n:
                if q == '"' and line[j] == "\\" and j + 1 < n and line[j + 1] in ('"', "\\"):
                    j += 2
                    continue
                if line[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


# Heredoc 开启符：`<<EOF`（不带引号 → bash 会在正文中执行命令替换）对比
# `<<'EOF'` / `<<"EOF"`（带引号 → 正文按字面处理）。分组 1 捕获引号字符，
# 分组 2 捕获分隔符。
def _find_heredoc_start(line: str) -> tuple[str, bool] | None:
    """当 *line* 在引号外打开 heredoc 时，返回（分隔符, 是否带引号）。

    扫描原始行并跟踪引号状态，因此引号字符串内的 `<<EOF`（`echo "x <<EOF"`）
    不会被检测到，后续行仍作为命令校验。支持 `<< EOF`、`<<EOF`、`<<"EOF"`、
    `<<'EOF'` 以及 `<<-` 去制表符形式；`<<<` herestring 不是 heredoc。该行
    未打开 heredoc 时返回 None。
    """
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                c = line[i]
                # \" 不会闭合双引号字符串
                if quote == '"' and c == "\\" and i + 1 < n and line[i + 1] in ('"', "\\"):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2  # 转义字符不可能开启 heredoc
            continue
        if ch == "$" and i + 1 < n:
            if line[i + 1] == "{":
                # ${...} 的数组下标里可能含算术（`${arr[1 << 2]}`）——
                # 里面的 << 不能打开 heredoc。
                end = _skip_braced(line, i + 2)
                if end is None:
                    return None  # 未闭合的 ${——语法错误，无 heredoc
                i = end + 1
                continue
            if line[i + 1] == "[":
                end = line.find("]", i + 2)
                if end == -1:
                    return None  # 未闭合的 $[——语法错误，此处无 heredoc
                i = end + 1
                continue
            if line[i + 1] == "(" and i + 2 < n and line[i + 2] == "(":
                end = _skip_arith_parens(line, i + 3)
                if end is None:
                    return None  # 未闭合的 $((——语法错误，此处无 heredoc
                i = end + 1
                continue
        if ch == "(" and i + 1 < n and line[i + 1] == "(" and _at_command_start(line, i):
            end = _skip_arith_parens(line, i + 2)
            if end is None:
                return None  # 未闭合的 ((——语法错误，此处无 heredoc
            i = end + 1
            continue
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            j = i + 2
            if j < n and line[j] == "<":
                i = j + 1  # <<< herestring——不是 heredoc
                continue
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j >= n:
                return None
            if line[j] in ("'", '"'):
                q = line[j]
                end = line.find(q, j + 1)
                if end == -1:
                    return None
                # 失败时关闭：bash 会对分隔符做引号移除，因此闭合引号之后的
                # 任何内容（`<<"EO"F`）都会让真实分隔符变得有歧义。把它当作
                # 非 heredoc 处理，并把后续行作为普通命令校验。
                rest = line[end + 1:].lstrip(" \t")
                if rest and rest[0] not in ";|&<>":
                    return None
                return line[j + 1:end], True
            if line[j] == "\\":
                # \EOF 是带引号的分隔符（bash 把正文按字面处理，与 <<'EOF'
                # 相同）——返回 quoted=True，使正文中的替换载荷不被当作真实
                # 命令。失败时关闭：bash 对此形式也做引号移除（`<<\EO\F` 的
                # 分隔符是 EOF），因此第一个之后仍含反斜杠/引号的分隔符无法
                # 可靠匹配——返回 None，并把后续每一行作为普通命令校验。
                j += 1
                start = j
                while j < n and line[j] not in " \t;|&<>":
                    j += 1
                delim = line[start:j]
                if j == start or "\\" in delim or "'" in delim or '"' in delim:
                    return None
                return delim, True
            start = j
            while j < n and line[j] not in " \t;|&<>":
                j += 1
            if j == start:
                return None
            return line[start:j], False
        i += 1
    return None


def ends_with_continuation(text: str) -> bool:
    """当 *text* 以转义换行的反斜杠结尾时为真。

    能感知引号：单引号内反斜杠是字面数据，换行仍是字符串的一部分；顶层和
    双引号内 ``\\`` + 换行是 bash 会移除的行续接。偶数个结尾反斜杠意味着
    最后一个本身被转义（``a\\\\`` → 字面 ``a\\``，无续接）。
    """
    if not text.endswith("\\"):
        return False
    in_single = in_double = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            if i == n - 1:
                # 行尾的转义起始符——它会吃掉换行符。
                return True
            i += 2  # 被转义的字符（含反斜杠/引号）是数据
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = not in_double
        i += 1
    return False


def split_logical_lines(command: str) -> list[tuple[str, bool]]:
    """把 *command* 拆成逻辑行：``[(文本, 是否为 heredoc 正文), ...]``。

    shell 中换行分隔命令，但 shlex 把它当作普通空白——``ls\\nrm -rf keep``
    会被分词为一条命令，其参数是 ``rm -rf keep``，于是允许名单（首 token
    ``ls``）和逐参数路径检查都通过，而 bash 实际运行了 ``rm``。因此调用方
    必须把每条逻辑行作为独立命令校验。

    引号仍未闭合的物理行（引号字符串可能跨多行）和以续接反斜杠结尾的行会
    合并。续接合并会像 bash 一样移除反斜杠-换行对——保留它们会让 shlex 把
    字面换行粘进下一个 token（``"\\n../x"`` 以换行开头，因此既不是 ``..``
    也不是绝对路径），路径检查会放行 ``cat \\`` + ``../sibling/.env``，而
    bash 执行了该逃逸。heredoc 正文会被标记：bash 把它们作为数据而非命令
    喂给命令的 stdin——不过不带引号的正文仍会执行命令替换，调用方对整条命令
    做此检查。
    """
    out: list[tuple[str, bool]] = []
    buffer = ""
    # 缓冲区挂起的原因：续接合并会丢弃反斜杠 + 换行；引号跨行合并保留换行
    # （它是字符串数据）。
    buffer_continued = False
    heredoc_delim: str | None = None
    for raw in command.split("\n"):
        if heredoc_delim is not None:
            out.append((raw, True))
            if raw.strip() == heredoc_delim:
                heredoc_delim = None
            continue
        if buffer:
            if buffer_continued:
                candidate = buffer[:-1] + raw
            else:
                candidate = f"{buffer}\n{raw}"
        else:
            candidate = raw
        # has_unclosed_quote 只报告引号错误；以未加引号/单引号外反斜杠结尾的
        # 行，反斜杠转而转义换行（shlex 会抛出 "No escaped character"），把
        # 下一行并入本命令。
        if has_unclosed_quote(candidate) or ends_with_continuation(candidate):
            buffer_continued = ends_with_continuation(candidate)
            buffer = candidate
            continue
        buffer = ""
        out.append((candidate, False))
        heredoc = _find_heredoc_start(candidate)
        if heredoc is not None:
            heredoc_delim = heredoc[0]
    if buffer:
        if buffer_continued:
            # bash 会丢弃输入末尾悬空的续接符，并运行已有内容
            # （`printf 'echo hi \' | bash` 打印 `hi`）。
            out.append((buffer[:-1], False))
        else:
            # 输入末尾未闭合的引号——原样发出，让校验失败时关闭，而不是静默
            # 丢弃尾部。
            out.append((buffer, False))
    return out


def _check_all_substitutions(
    text: str, *, cwd: Path, root: Path, single_quotes_literal: bool = True
) -> str | None:
    """在每一个嵌套层级校验命令替换载荷。

    单层捕获只能拿到最内层的 $(...)——bash 会执行完整的嵌套链，因此
    `echo "$(cat $(echo /etc/passwd))"` 会读取主机文件，除非每一层都单独
    校验。每一层都作为独立命令校验，且其内部替换仍保留在原位——随后 token
    检查会在外层的路径位置看到一个替换（`cat $(echo /etc/passwd)` 解析为
    `cat $` → 被拒绝）。

    命令行上单引号区域是字面量（``'$(cat /etc/passwd)'`` 只会打印，绝不
    执行），会跳过；不带引号的 heredoc 正文保留 bash 的真实语义——那里引号
    只是普通字符，因此调用方传 single_quotes_literal=False 来扫描它们。
    """
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'" and single_quotes_literal:
            end = text.find("'", i + 1)
            if end == -1:
                break  # 未终止——bash 语法错误，失败时关闭
            i = end + 1
            continue
        if c == "`":
            end = text.find("`", i + 1)
            if end == -1:
                break
            payload = text[i + 1 : end]
            reason = _validate_substitution_payload(payload, cwd=cwd, root=root)
            if reason:
                return reason
            i = end + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            close = _skip_subst_parens(text, i + 2)
            if close is None:
                return "Unclosed $(...); refusing to run"
            payload = text[i + 2 : close]
            reason = _validate_substitution_payload(payload, cwd=cwd, root=root)
            if reason:
                return reason
            i = close + 1
            continue
        i += 1
    return None


def _validate_substitution_payload(payload: str, *, cwd: Path, root: Path) -> str | None:
    """把一个 $(...) 载荷作为独立命令校验。"""
    payload = payload.strip()
    if not payload:
        return None
    if payload.startswith("<"):
        return "Command substitution reading a file ($(< ...)) is not allowed"
    # 载荷本身可能再嵌套更多替换——validate_command 会针对那些重新进入
    # _check_all_substitutions。
    return validate_command(payload, cwd=cwd, project_root=root, allow_substitution=True)

_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "add", "commit", "push", "pull", "fetch",
    "checkout", "switch", "restore", "branch", "tag", "stash", "blame",
    "rev-parse", "ls-files", "merge", "init",
    # 常见工作流子命令——都在仓库内部操作；路径参数仍要通过下方逐 token 的
    # 路径校验。
    "reset", "rm", "mv", "revert", "rebase", "clean", "cherry-pick", "cherry",
    "apply", "am", "config", "submodule", "worktree", "reflog", "rev-list",
    "describe", "shortlog", "count-objects", "ls-remote", "grep", "show-ref",
    "symbolic-ref", "for-each-ref", "ls-tree", "cat-file", "name-rev",
    "archive", "bisect", "notes", "range-diff", "remote",
})
# 会重定向仓库/工作树位置，或运行任意外部命令的 git 选项——在 git 调用中
# 的任何位置都拒绝。
_GIT_FORBIDDEN_LONG = ("--git-dir", "--work-tree", "--exec-path")
# 会写到项目之外（~/.gitconfig、/etc）的 `git config` 作用域。
_GIT_CONFIG_FORBIDDEN_SCOPES = frozenset({"--global", "--system"})

# 其值会在之后、由另一条命令执行的配置键：core.fsmonitor 在每次 status
# （含 git_repo 工具的整洁度检查）时运行，filter.* 在 add/checkout 时运行，
# alias.* 在调用时运行，core.pager/sshCommand/hooksPath/difftool.*/
# mergetool.* 在各自触发时运行，include.path 读取其他配置文件。`git -c`
# 检查会拒绝这些；`git config key value` 的写入路径也必须拒绝——该值落入
# .git/config 后，无需本命令再次运行就会触发。
_GIT_CONFIG_EXECUTABLE_KEY_RE = re.compile(
    r"^(?:alias\.|core\.(?:pager|fsmonitor|sshCommand|hooksPath|editor)|"
    r"credential\.helper|filter\.|difftool\.|mergetool\.|include\.path|"
    r"diff\.(?:external|[A-Za-z0-9_.-]+\.(?:command|textconv)))",
    re.IGNORECASE,
)
_NODE_EVAL_OPTS = frozenset({"-e", "--eval", "-p", "--print"})

# 参数全部是数据、绝不会是文件路径的命令。
_DATA_COMMANDS = frozenset({"echo", "printf", "pwd", "tr", "true", "false"})

# 第一个位置参数是数据（模式/程序），其余位置参数是文件路径的命令
# （grep <模式> <文件...>）。
_FIRST_POS_DATA = frozenset({"grep", "egrep", "fgrep", "jq", "awk", "gawk", "sed"})

# 位置路径操作数是写入目标的命令。它们的 .git 操作数会被拒绝——git 之后会
# 执行写入其中的钩子/配置。sed -i 和 gawk -i inplace 在选项循环中单独处理
# （只有其就地模式才写入）。
_GIT_WRITE_OPERAND_COMMANDS = frozenset({"cp", "mv", "mkdir", "touch"})

# 各命令中独立的值 token 是数据而非路径的选项（如
# `git commit -m "mentions ../x"` 不应触发路径检查）。
_DATA_VALUE_OPTS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "-m", "-b", "-t", "-u", "--message", "--author", "--date",
        "--format", "--pretty", "--abbrev", "--depth", "-j", "--jobs",
        "--grep", "-i", "-e",
    }),
    "grep": frozenset({"-e", "--regexp", "--label"}),
    "find": frozenset({
        "-name", "-iname", "-path", "-ipath", "-regex", "-regextype", "-type",
        "-size", "-user", "-group", "-perm", "-mtime", "-mmin", "-ctime",
        "-cmin", "-atime", "-amin", "-maxdepth", "-mindepth", "-printf",
    }),
    "awk": frozenset({"-F", "-v", "--field-separator", "--assign"}),
    "gawk": frozenset({"-F", "-v", "--field-separator", "--assign"}),
    "sed": frozenset({"-e", "--expression"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields", "--characters", "--bytes"}),
    "sort": frozenset({"-t", "-k", "--field-separator", "--key"}),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "--pid", "-s", "--sleep-interval"}),
    "jq": frozenset({"--arg", "--argjson", "--raw-input", "--indent", "-n"}),
    "python": frozenset({"-c", "-m", "-W", "-X"}),
    "node": frozenset({"-e", "-p", "--eval", "--print"}),  # 反正会被 eval 扫描拒绝
}

# 各命令中独立的值 token 是需要校验的路径的选项。
_PATH_VALUE_OPTS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-F", "--file"}),
    "jq": frozenset({"-f", "--from-file"}),
    "awk": frozenset({"-f"}),
    "gawk": frozenset({"-f"}),
    "sed": frozenset({"-f", "--file"}),
    "grep": frozenset({"-f", "--file"}),
    "sort": frozenset({"-o", "--output"}),
    "find": frozenset({"-newer", "-anewer", "-cnewer"}),
}


def _cmd_key(cmd_base: str) -> str:
    """为选项表归一化命令基名（python3.12 -> python）。"""
    return "python" if _PY_RE.match(cmd_base) else cmd_base


# git -c 的值必须是惰性字面量：无空白（core.pager='cat ../x' 会通过 shell
# 分词执行）、无 '!'（alias.!cmd）、无 shell 元字符（; | & > < ` $）。真实的
# 配置需求——user.name、core.autocrlf、commit.gpgsign、init.defaultBranch——
# 总是普通字面量。
_SAFE_GIT_C_RE = re.compile(r"^[A-Za-z0-9_.-]+=[A-Za-z0-9_.:/@+=-]*$")

# 值可合法含空格的配置键（git -c user.name="First Last"）。这些键是惰性的
# 文本/布尔值——git 从不执行它们——因此含空格的值只要不带 shell 元字符和
# 逃逸路径就是安全的。易执行键（core.pager、alias.*、credential.helper 等）
# 刻意不在此列表，所以带引号的 `git -c core.pager='cat ../x'` 仍会落到拒绝。
_GIT_C_SPACE_OK_KEYS = frozenset({
    "user.name", "user.email", "commit.template",
    "init.defaultbranch", "core.autocrlf", "core.eol", "core.safecrlf",
})
# 能在 shlex 引号移除后留存、让含空格的 -c 值变得危险的 shell 元字符
# （分词、替换、别名）。
_GIT_C_DANGEROUS_VALUE_RE = re.compile(r"[;|&><`$!\\'\"]")

# 可执行配置键允许持有的裸命令名：把输入作为数据加载的分页器、查看器和编辑
# 器，以及惰性的是/否辅助命令。裸名字并不自动惰性——git 会用 git 控制的输入
# 执行这些值：shell 分页器在 stdin 收到提交日志（``core.pager=sh`` 会运行
# 植入的提交消息行），shell 编辑器把消息文件作为 argv（``core.editor=sh
# commit --amend`` 会运行上一条 ``-m`` 消息），过滤驱动在 stdin 收到文件内容
# （``filter.pwn.clean=sh`` 会运行暂存文件）。解释器名的黑名单无法保持完整，
# 因此可接受集合是封闭的。
_GIT_C_INERT_COMMANDS = frozenset({
    "cat", "less", "more", "most", "bat",
    "vi", "vim", "nvim", "nano", "pico", "emacs", "micro",
    "true", "false",
})


def _is_safe_git_c_value(value: str) -> bool:
    # 配置值（第一个 '=' 之后）不得夹带可执行路径：`core.pager=../evil`
    # ——git 相对仓库根执行分页器，于是 '../evil' 逃逸出项目。对完整
    # "name=value" 字符串做 _check_simple 会漏掉它（'..' 嵌在 name 段里）；
    # 因此直接检查值部分。类 URL 值（remote.origin.url=https://...）仍可
    # 工作——它们以协议开头，而非 '/'、'~' 或 '..'。
    _name, _eq, val = value.partition("=")
    if not _eq:
        return False
    # diff.external / diff.<driver>.command|textconv 会把被比较的文件作为参数
    # 运行该值——`git config diff.external sh` 会让 git 把每个旧/新文件当作脚本
    # 执行。直接拒绝整个家族（这里即使裸命令名也不安全）；惰性的
    # diff.renames/color 等不匹配，继续工作。
    if re.fullmatch(
        r"diff\.external|diff\.[A-Za-z0-9_.-]+\.(?:command|textconv)", _name,
        re.IGNORECASE,
    ):
        return False
    # 可执行键（过滤器、分页器、fsmonitor、别名等）把值作为命令运行。只有
    # 上方封闭的惰性裸名集合合格：路径分隔符或开头的 '.' 会解析到一个内容从未
    # 被校验的项目脚本（`git -c core.fsmonitor=./evil.sh status` 在每次 status
    # 时执行它，即经 git 触发的 ./script 逃逸），而裸 shell/解释器名会执行
    # git 控制的输入（见 _GIT_C_INERT_COMMANDS）。
    if _GIT_CONFIG_EXECUTABLE_KEY_RE.match(_name):
        lowered = _name.lower()
        if lowered.startswith("alias."):
            # 非 `!` 别名总是展开为 `git <value>`——git 从不把它们交给
            # shell——因此任何裸名在那里都是惰性的（`!` 前缀的 shell 别名被
            # 裸名正则排除，与之前相同）。
            return bool(re.fullmatch(r"[A-Za-z0-9_-]+", val)) and _check_simple(val) is None
        if lowered.startswith("include"):
            # include.path / includeIf.* 加载一个内容从不重新校验的配置文件
            # ——被包含的 `core.pager = sh` 会复活这里拒绝的每个逃逸。写入路径
            # 已直接拒绝该键；调用作用域形式也必须拒绝。
            return False
        return val in _GIT_C_INERT_COMMANDS
    if _SAFE_GIT_C_RE.match(value):
        return _check_simple(val) is None
    # 含空白的值（user.name="First Last"）：只用于 git 从不执行的键，且仅当
    # 值不含 shell 元字符时。
    if _name.lower() in _GIT_C_SPACE_OK_KEYS and not _GIT_C_DANGEROUS_VALUE_RE.search(val):
        return _check_simple(val) is None
    return False


def _starts_comment(line: str, i: int) -> bool:
    """当 *i* 处的 ``#`` 开启注释而非位于单词内部时为真（``x#y`` 是字面
    数据）。"""
    if i == 0:
        return True
    return line[i - 1] in " \t;&|()<>"


def _scan_brace_group(line: str, start: int) -> tuple[int | None, bool]:
    """扫描 *start* 处（指向 ``{`` 之后）打开的 ``{...}`` 组。

    返回 ``(闭合索引, 是否扩展)``。当组内含未加引号的 ``,`` 或 ``..`` 且没有
    未加引号的空白时，*expands* 为真——这正是 bash 会重写成多个单词的形态。
    花括号永不闭合时，``closing_index`` 为 None。引号和 ``${...}`` /
    ``$(...)`` / ``$((...))`` 区域会跳过：其内容是数据或单独校验。
    """
    depth = 1
    n = len(line)
    i = start
    saw_sep = False
    saw_space = False
    while i < n:
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n:
                if quote == '"' and line[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "$" and i + 1 < n:
            if line[i + 1] == "{":
                end = _skip_braced(line, i + 2)
                if end is None:
                    return None, False
                i = end + 1
                continue
            if line[i + 1] == "(":
                if i + 2 < n and line[i + 2] == "(":
                    end = _skip_arith_parens(line, i + 3)
                else:
                    end = _skip_subst_parens(line, i + 2)
                if end is None:
                    return None, False
                i = end + 1
                continue
        if c in " \t":
            saw_space = True
        elif c == ",":
            saw_sep = True
        elif c == "." and i + 1 < n and line[i + 1] == ".":
            saw_sep = True
            i += 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i, saw_sep and not saw_space
        i += 1
    return None, False


def _check_unquoted_expansions(line: str) -> str | None:
    """拒绝未加引号的花括号扩展和进程替换。

    校验看到的是 bash 重写之前的命令：``cat {/,}etc/passwd`` 被分词为一个无害
    的相对单词，之后才变成 ``cat /etc/passwd``；而 ``<(cmd)`` / ``>(cmd)``
    运行其载荷并拼接进一个路径。两种形式都无法用 token 级路径检查判断，因此
    失败时关闭。引号文本是字面量，``#`` 注释是惰性的，``${...}`` /
    ``$(...)`` 在别处处理（替换载荷由 :func:`_check_all_substitutions` 递归
    校验）。
    """
    reason = "Process substitution (<(...) or >(...)) is not allowed"
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n:
                if quote == '"' and line[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "#" and _starts_comment(line, i):
            break
        if c in "<>" and i + 1 < n and line[i + 1] == "(":
            return reason
        if c == "$" and i + 1 < n:
            if line[i + 1] in ("'", '"'):
                # ANSI-C 引号只有在 bash 解析单词时才解码转义；shlex 剥掉引号，
                # 把字面 `\x2f...` 文本交给检查，于是 `$'\x2fetc\x2fpasswd'`
                # 在运行时变成绝对路径。引号区域被上方循环跳过，因此这里命中的
                # 是真正的 ANSI-C 引号。
                return (
                    "ANSI-C quoting ($'...' or $\"...\") is not allowed: it "
                    "decodes characters after validation, so the resulting "
                    "path never reaches the checks"
                )
            if line[i + 1] == "{":
                end = _skip_braced(line, i + 2)
                i = end + 1 if end is not None else i + 1
                continue
            if line[i + 1] == "(":
                if i + 2 < n and line[i + 2] == "(":
                    end = _skip_arith_parens(line, i + 3)
                else:
                    end = _skip_subst_parens(line, i + 2)
                i = end + 1 if end is not None else i + 1
                continue
        if c == "{":
            end, expands = _scan_brace_group(line, i + 1)
            if expands:
                return (
                    "Brace expansion ({a,b} or {x..y}) is not allowed: it "
                    "changes path arguments after validation"
                )
            # 这一层不是扩展组：走进其内部，使嵌套组（`{{a,b} c}`）仍被捕获。
            i += 1
            continue
        i += 1
    return None


def has_unclosed_quote(text: str) -> bool:
    """当 shlex 因引号未闭合而无法对 *text* 分词时为真。

    供 code_executor 在校验前把物理行组合成逻辑行（引号字符串可合法地跨
    多行）。
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        list(lexer)
        return False
    except ValueError as exc:
        return "quotation" in str(exc)


def validate_command(
    command: str,
    *,
    cwd: Path,
    project_root: Path,
    allow_substitution: bool = False,
    tainted_vars: frozenset[str] | None = None,
    pre_assigned_vars: set[str] | None = None,
) -> str | None:
    """逐 token 校验 shell 命令。返回 None 表示允许，否则返回拒绝原因。

    仅做校验：实际执行的仍是原始命令字符串，因此解析失败会失败时关闭。
    ``cwd`` 必须已由调用方解析到 ``project_root`` 之内。

    除非 *allow_substitution* 为真，否则命令替换（``$(...)``/反引号）和变量
    展开路径（``$var/x``）一律禁止：嵌套或展开的值可能把路径作为数据夹带
    出去（``cat $(printf '../x')``、``p=../proj-b; cat $p/x``），token 级校验
    永远看不到。开头的 ``VAR=value`` 赋值本身也要校验（其值之后可能被展开），
    因此即使赋值不触碰任何文件，``p=../proj-b`` 也会被拒绝。code_executor 的
    bash 模式允许替换，因为其 _BLOCKED_BASH 正则会剥掉可能滥用它的外部命令，
    其环境变量都指向项目内部，且原始内部文本仍由下方检查（粗略地）分词。

    路径检查能感知命令：引号数据字符串（提交消息、grep 模式、``-c`` 代码）
    通过各命令的选项表跳过，而真实路径参数（文件命令的位置参数、重定向目标、
    git -C 值等）仍被限制在项目内——重定向目标还可位于系统临时目录。
    """
    if not allow_substitution and ("$(" in command or "`" in command):
        return "Command substitution ($(...) or backticks) is not allowed"

    cwd = Path(cwd).resolve()
    root = Path(project_root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()

    # 每个替换载荷——在每个嵌套层级——都必须作为独立命令通过校验。单层扫描
    # 会漏掉嵌套 $(...) 的外层，而 bash 仍会执行它们（若外层 `cat ...` 从不
    # 检查，echo "$(cat $(echo /etc/passwd))" 会读取主机文件）。对整条命令
    # 运行，因此也覆盖会执行替换的、不带引号的 heredoc 正文。
    if allow_substitution and ("$(" in command or "`" in command):
        reason = _check_all_substitutions(command, cwd=cwd, root=root)
        if reason:
            return reason

    # 本命令字符串中更早赋值的变量名。只有这些可以出现在后续路径参数（$f）
    # 中——它们的值在赋值处已校验。主机环境变量（$HOME、$PATH）可展开为任何
    # 东西。pre_assigned_vars 从同一脚本更早的行（code_executor 的逐行校验器）
    # 播种集合：第 1 行的赋值在第 3 行仍生效，其值在该行通过时已检查。
    assigned_vars = set(pre_assigned_vars or ())

    # 每条逻辑行是独立命令：一次对整串分词，会让后一行的命令词被读成第一行
    # 命令的参数（echo ok\ncat /etc/passwd 通过所有检查，因为 `cat` 和
    # `/etc/passwd` 是 echo 的数据参数）。heredoc 正文是 stdin 数据，不是命令
    # ——但不带引号的分隔符仍会在正文内执行命令替换（那里引号是普通字符），
    # 因此不带引号的正文行在下方获得自己的替换扫描；带引号的分隔符
    # （<<'EOF'）是字面量，保持跳过。
    body_quoted: bool | None = None
    for line, is_heredoc_body in split_logical_lines(command):
        if is_heredoc_body:
            if body_quoted is False:
                # 上方整条命令扫描会跳过 '...' 区域，且从不会暴露藏在引号合并
                # 行里的 heredoc（`echo "$(cat <<E ...)`——未闭合引号吞掉其余
                # 部分），于是正文中的 '$(cat /etc/passwd)' 会执行且输出到达
                # stdout。把引号当作字面字符，扫描每个不带引号的正文行。
                reason = _check_all_substitutions(
                    line, cwd=cwd, root=root, single_quotes_literal=False
                )
                if reason:
                    return reason
            continue
        # 以 split_logical_lines 相同的方式（相同函数、相同行文本）重新推导当前
        # 正文运行的带引号性：该标志一直保持，直到下一条非正文逻辑行打开（或
        # 清除）heredoc。
        heredoc = _find_heredoc_start(line)
        body_quoted = heredoc[1] if heredoc is not None else None
        # 花括号扩展和进程替换在分词后重写命令，因此其结果永远到不了下方的
        # 路径检查。
        reason = _check_unquoted_expansions(line)
        if reason:
            return reason
        # 转义括号是字面参数（`find . \( -true \) -delete`），但 posix 模式的
        # shlex 会丢弃反斜杠并产出 "(" / ")"。下方分隔符拆分器随即把它们当作
        # 命令边界，使 -delete/-exec 变成某个段的命令词并跳过 find 守卫。用
        # 哨兵保持转义 token 完整；shell 仍收到原始文本。
        tokenized = (
            line.replace("\\(", "\x01").replace("\\)", "\x02")
            if "\\(" in line or "\\)" in line
            else line
        )
        try:
            lexer = shlex.shlex(tokenized, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError as exc:
            return f"Command could not be parsed safely ({exc}); refusing to run"
        for segment in _split_segments(tokens):
            reason = _validate_segment(
                segment, cwd=cwd, root=root, temp=temp,
                allow_substitution=allow_substitution,
                assigned_vars=assigned_vars, tainted_vars=tainted_vars,
            )
            if reason:
                return reason
    return None


# 可能合并成一个 shlex token 但仍充当分隔符的字符——shlex 把 `);` 粘成单个
# token，把它当作参数会让后续命令并入前一个段的数据参数列表，完全跳过其
# 路径检查。
_SEPARATOR_CHARS = frozenset(";&|()")


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """按命令分隔符（管道、&&、; 等）拆分 token 流。"""
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS or (
            tok and all(c in _SEPARATOR_CHARS for c in tok)
        ):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _validate_segment(
    tokens: list[str], *, cwd: Path, root: Path, temp: Path,
    allow_substitution: bool, assigned_vars: set[str],
    tainted_vars: frozenset[str] | None = None,
) -> str | None:
    """校验一个命令段（命令及其参数/重定向）。"""
    i = 0
    # 开头的 VAR=value 赋值是环境设置，不是路径——但其值之后可能经 $VAR 展开，
    # 因此它本身不能是逃逸路径（如 `p=../proj-b; cat $p/x`）。
    while i < len(tokens) and _ASSIGN_RE.match(tokens[i]):
        reason = _check_assignment(tokens[i], allow_dollar=allow_substitution,
                                   assigned_vars=assigned_vars)
        if reason:
            return reason
        # 只有值非空的名字才获得"已赋值"状态：`x=` 在运行时展开为空，于是
        # `cat $x/etc/hostname` 静默变成 `cat /etc/hostname`——一个绝对路径
        # ——逃过检查。从命令替换捕获的值也不跟踪：在嵌套载荷中
        # （`echo "$(x=$(cat p); cat $x)"`），捕获的文本是任意项目数据，
        # 因此 `$x` 之后会解析到赋值检查从未见过的任意路径。shlex 的标点拆分
        # 把 `x=$(...)` 变成 token `x=$`（左括号开启下一段），因此以裸 `$`
        # 结尾的值也是一种拆分替换。
        value = tokens[i].split("=", 1)[1]
        if value and "$(" not in value and "`" not in value and not value.endswith("$"):
            assigned_vars.add(tokens[i].partition("=")[0])
        i += 1
    if i >= len(tokens):
        return None

    cmd = tokens[i]
    # 在允许替换的模式（code_executor 的 bash 分支）下没有命令白名单——黑名单是
    # 唯一关卡，且扫描的是字面文本。运行时构建的命令词（`B=rl; A=cu$B;
    # $A ...`，或 `$(echo curl)`）不含任何被阻止的子串，却能展开成任意程序，
    # 因此拒绝命令位置的每一次展开。
    if allow_substitution and ("$" in cmd or "`" in cmd):
        return (
            f"Command {cmd!r}: a command name built from a variable or "
            "command substitution cannot be checked against the blocked "
            "command list; use a literal command"
        )
    # ./ 前缀命令会执行一个内容从未被校验的项目内脚本——
    # `echo 'cat /etc/passwd' > run; chmod +x run; ./run` 把任意主机读取夹带过
    # 所有检查（`. run` 点形式被 code_executor 的 _BLOCKED_BASH 阻止；裸
    # `./run` 被校验为普通项目内路径，然后在 bash 模式下无守卫执行）。只有
    # ./mvnw、./gradlew 构建包装器继续允许执行。
    if cmd.startswith("./") and cmd not in ("./mvnw", "./gradlew"):
        return (
            f"Command {cmd!r}: executing in-project scripts is not allowed; "
            "the shell tool's allowlist covers script execution"
        )
    if cmd not in _ALLOWED_ABSOLUTE_COMMANDS:
        reason = _check_path(cmd, cwd=cwd, root=root, allow_dollar=allow_substitution,
                             assigned_vars=assigned_vars, tainted_vars=tainted_vars)
        if reason:
            return reason
    cmd_base = cmd.replace("\\", "/").rsplit("/", 1)[-1]  # ./mvnw -> mvnw（取基名）
    key = _cmd_key(cmd_base)
    is_awk_sed = cmd_base in ("awk", "gawk", "sed")
    is_pattern_cmd = cmd_base in ("grep", "egrep", "fgrep")
    is_jq = cmd_base == "jq"
    data_opts = _DATA_VALUE_OPTS.get(key, frozenset())
    path_opts = _PATH_VALUE_OPTS.get(key, frozenset())
    is_python = bool(_PY_RE.match(cmd_base))
    is_node = cmd_base == "node"
    is_git = cmd_base == "git"
    is_find = cmd_base == "find"
    pattern_opts: frozenset[str] = frozenset()
    if is_pattern_cmd:
        pattern_opts = frozenset({"-e", "--regexp", "-f", "--file"})
    elif is_jq:
        pattern_opts = frozenset({"-f", "--from-file"})
    i += 1

    # 改变变量的内建命令，从校验器从不检查的数据中赋值——stdin
    # （read/mapfile/getopts）或 printf 的格式参数。早先被赋予安全字面量的
    # 名字，随后会保持"已赋值"状态，而其值变成任意路径：`x=safe; printf -v x
    # '\x2fetc\x2fpasswd'; cat $x` 会读取绝对路径。`unset` 清空被跟踪的名字，
    # 使 `$x/etc` 塌缩成绝对路径。
    if cmd_base in ("read", "mapfile", "readarray", "getopts", "unset"):
        return (
            f"{cmd_base} is not allowed: it assigns variables from data the "
            "validator never sees, so a later unquoted $VAR expansion could "
            "smuggle an absolute path past the assignment checks; use "
            "explicit literal arguments instead"
        )
    if cmd_base == "printf":
        # printf 本身是有用的数据命令；只有 -v（赋值给变量）会重新定义 token
        # 检查看不到的值。
        for tok in tokens[i:]:
            if tok == "--" or not tok.startswith("-"):
                break
            if tok.startswith("-v"):
                return (
                    "printf -v is not allowed: assigning through printf "
                    "stores a value the validator never sees, so a later "
                    "unquoted $VAR expansion could smuggle an absolute path; "
                    "plain printf without -v is fine"
                )
    if cmd_base in ("declare", "typeset", "local"):
        # `declare -n b=PATH` 让之后对 b 的赋值改写 PATH，而 PATH 是赋值检查
        # 看不到的执行控制变量。
        for tok in tokens[i:]:
            if tok == "--" or not tok.startswith("-"):
                break
            if "n" in tok[1:]:
                return (
                    f"{cmd_base} nameref (-n) is not allowed: it can alias a "
                    "later assignment onto PATH or another execution-control "
                    "variable, which the assignment checks cannot see"
                )

    if cmd_base in ("export", "local", "declare", "typeset", "readonly"):
        # `export NAME=value`（及 declare 家族）与开头的 VAR=value 前缀是同一
        # 种赋值——`export PATH=/tmp/evil` 不能作为路径参数溜过（对白名单命令
        # 的 PATH 劫持：PATH=./evil 让下一个 `git` 运行内容从未被校验的项目内
        # 脚本）。值像任何赋值一样校验；只有当名字的值是普通字面量时，才把它
        # 跟踪为已赋值——含 `$` 的值会被分词成碎片（`x=$(echo ...)` ->
        # `x=$` `(` ...），否则会躲过替换守卫。裸名（`export FOO`）是惰性的：
        # 值是当前环境的，因此该名字不跟踪，之后路径位置的 $FOO 仍被拒绝。
        # 名字之后的重定向由下方参数循环校验。
        while i < len(tokens) and _ASSIGN_RE.match(tokens[i]):
            reason = _check_assignment(tokens[i], allow_dollar=allow_substitution,
                                       assigned_vars=assigned_vars)
            if reason:
                return reason
            value = tokens[i].split("=", 1)[1]
            if value and "$" not in value:
                assigned_vars.add(tokens[i].partition("=")[0])
            i += 1

    skip_next = False        # 上一个选项把下一个 token 作为数据消费
    first_pos_seen = False   # 用于 _FIRST_POS_DATA 命令
    # 对 awk/sed：表达式来自 -e/--expression/-f/--file，因此第一个位置参数是
    # 文件操作数，而不是程序字符串。否则 `sed -e 's/^/x/' /etc/passwd` 会把
    # "/etc/passwd" 当作（做空值检查的）程序扫描，从不对它做路径校验——任意
    # 文件读取。
    program_seen = False
    # sed -i / gawk -i inplace 就地改写其文件操作数，使这些操作数成为下方
    # .git 检查的写入目标。
    in_place = False
    # 对 grep/egrep/fgrep/jq：模式/程序来自选项（-e/--regexp/-f/--file/
    # --from-file），因此第一个位置参数是文件操作数而非数据——否则
    # `grep -e foo ../sibling/secret` 会读到项目之外。
    pattern_seen = False
    git_subcommand: str | None = None
    git_config_key: str | None = None  # `git config` 的第一个位置参数
    pip_seen = False  # `python3 -m pip`（或粘连的 `-mpip`）——在下方按子命令设卡

    while i < len(tokens):
        tok = tokens[i]

        if tok in _REDIRECT_SKIP_OPS:
            i += 2  # heredoc 分隔符 / 文件描述符编号——不是路径
            continue
        if tok in _REDIRECT_PATH_OPS:
            if i + 1 >= len(tokens):
                return f"Redirection {tok!r} without a target"
            reason = _check_redirect(
                tokens[i + 1], cwd=cwd, root=root, temp=temp,
                allow_dollar=allow_substitution, assigned_vars=assigned_vars, tainted_vars=tainted_vars,
            )
            if reason:
                return reason
            i += 2
            continue

        if skip_next:
            skip_next = False
            i += 1
            continue

        if tok.startswith("-") and tok != "-":
            opt_name = tok.split("=", 1)[0]
            if is_awk_sed and (
                opt_name.startswith("-i") or opt_name.startswith("--in-place")
            ):
                # sed -i/--in-place（后缀可能粘连：-i.bak）和 gawk -i
                # inplace 就地编辑其文件操作数——这些操作数是写入，不能指向
                # .git。
                in_place = True
            # `python3 -m pip ...`（含粘连的 `-mpip` 形式）——模块被下方数据
            # 选项的 skip_next 消费，因此在这里记录，以便在第一个位置参数处对
            # pip 子命令设卡。
            if is_python and opt_name.startswith("-m"):
                module = tokens[i + 1] if opt_name == "-m" and i + 1 < len(tokens) else opt_name[2:]
                if module == "pip":
                    pip_seen = True
            # find -exec/-execdir/-ok/-okdir 经 shell 执行任意命令。下方路径
            # 检查只覆盖 find 自己的参数——被执行命令的参数永远看不到。这是
            # 这些标志唯一的关卡（没有原始字符串黑名单：grep 模式可合法提及
            # "-exec"）。
            if is_find and opt_name in ("-exec", "-execdir", "-ok", "-okdir", "-delete"):
                return (
                    f"find {opt_name} is not allowed: it would execute "
                    "arbitrary commands or recursively delete the project "
                    "workspace on the server"
                )
            if is_awk_sed and opt_name in _PATH_VALUE_OPTS.get(key, frozenset()):
                return (
                    f"{cmd_base} {opt_name} is not allowed: a script-file program "
                    "would bypass the program-string escape scan"
                )
            if is_awk_sed and "=" not in tok:
                # 粘连选项形式：shlex 把 `-e'1p'` 合并成单个 token `-e1p`，
                # 其选项名不匹配任何表——其值随后被静默跳过，下一个位置参数
                # 被当作（做逃逸检查的）程序字符串而非路径扫描：
                # `sed -e'1p' /etc/passwd` 会读取任意文件。粘连的
                # -f/--file 程序像其分离形式一样拒绝；粘连的
                # -e/--expression/--source 程序则扫描其逃逸，并像分离形式一样
                # 设置 program_seen。
                if opt_name.startswith("-f") or opt_name.startswith("--file"):
                    return (
                        f"{cmd_base} attached {opt_name!r} is not allowed: a "
                        "script-file program would bypass the program-string "
                        "escape scan"
                    )
                for expr_opt in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                    if opt_name.startswith(expr_opt) and len(opt_name) > len(expr_opt):
                        reason = _check_program_string(tok[len(expr_opt):], cmd_base)
                        if reason:
                            return reason
                        program_seen = True
                        break
            if is_pattern_cmd or is_jq:
                # 模式/程序来自选项：`grep -e foo FILE`、`jq -f prog.jq FILE`
                # ——第一个位置参数于是成为必须通过路径检查的文件操作数，而不是
                # 作为数据跳过（`grep -e foo ../sibling/secret` 曾借此绕过）。
                # 粘连形式（-efoo、-fFILE、--regexp'foo'）把值粘到选项 token
                # 上；分离形式由下方数据/路径选项表消费。
                for pat_opt in pattern_opts:
                    if opt_name == pat_opt:
                        pattern_seen = True
                        break
                    if "=" not in tok and opt_name.startswith(pat_opt):
                        attached_val = tok[len(pat_opt):]
                        if pat_opt in ("-f", "--file", "--from-file"):
                            reason = _check_path(attached_val, cwd=cwd, root=root,
                                                 allow_dollar=allow_substitution,
                                                 assigned_vars=assigned_vars, tainted_vars=tainted_vars)
                            if reason:
                                return reason
                        pattern_seen = True
                        break
            # `-c` 可能是 `-c name=value` 或粘连的 `-cname=value`（旧版 git
            # 接受）。粘连形式被拆成 opt_name="-cname"，因此简单的 `== "-c"`
            # 检查会让它落入通用 "=" 分支，其值扫描只拒绝路径——
            # `git -calias.x='!cat ../sibling/.env' x` 就会通过。此检查只在
            # 子命令之前生效：`git commit -c HEAD~1`、`git grep -c foo`、
            # `git log -c`、`git branch -c old new` 是子命令自己的选项，不是
            # 配置。
            if is_git and git_subcommand is None and (opt_name == "-c" or opt_name.startswith("-c")):
                # `git -c name=value` 为本次调用设置一项配置。多数值是惰性字面
                # 量，但 alias.!/pager/filter/credential 值经 shell 执行——把
                # 它们当作普通数据，会让 `git -c alias.x='!cat ../sibling/.env'
                # x` 逃逸出项目。只接受普通字面量。
                if tok.startswith("-c") and "=" in tok:
                    # 粘连形式 `-ccore.autocrlf=true`：整个后缀是配置字符串
                    # ——按第一个 '=' 拆分，会把值从其名字上撕离并拒绝每个粘连
                    # 形式。
                    value = tok[2:]
                elif "=" in tok:
                    value = tok.split("=", 1)[1]
                elif i + 1 < len(tokens):
                    value = tokens[i + 1]
                    i += 1
                else:
                    return "git -c requires a value"
                if not _is_safe_git_c_value(value):
                    return (
                        "git -c value must be a plain literal (name=value with "
                        "no spaces, quotes or shell metacharacters); use repo "
                        "config instead"
                    )
                i += 1
                continue
            if "=" in tok:
                value = tok.split("=", 1)[1]
                if is_awk_sed and opt_name in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                    reason = _check_program_string(value, cmd_base)
                    if reason:
                        return reason
                    program_seen = True
                elif opt_name in path_opts:
                    reason = _check_path(value, cwd=cwd, root=root,
                                         allow_dollar=allow_substitution,
                                         assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                                         # sort -o/--output 会写入其值；其他路径
                                         # 选项是程序/URL 读取。
                                         deny_git=cmd_base == "sort")
                    if reason:
                        return reason
                elif opt_name not in data_opts:
                    if not allow_substitution and "$" in value:
                        vars_used = _dollar_vars(value)
                        if not vars_used or not all(v in assigned_vars for v in vars_used):
                            return (
                                f"Option {opt_name!r} value {value!r} uses a "
                                "variable that is not assigned in this command; "
                                "assign it first or use a literal path"
                            )
                    reason = _check_simple(value)
                    if reason:
                        return reason
            elif opt_name in path_opts:
                if i + 1 >= len(tokens):
                    return f"Option {opt_name!r} without a value"
                reason = _check_path(tokens[i + 1], cwd=cwd, root=root,
                                     allow_dollar=allow_substitution,
                                     assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                                     deny_git=cmd_base == "sort")
                if reason:
                    return reason
                i += 1  # 消费路径值
                if is_awk_sed:
                    # awk/sed -f/--file 提供程序文件——下一个位置参数是文件
                    # 操作数（见 program_seen）。
                    program_seen = True
            elif is_awk_sed and opt_name in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                if i + 1 >= len(tokens):
                    return f"Option {opt_name!r} without a value"
                reason = _check_program_string(tokens[i + 1], cmd_base)
                if reason:
                    return reason
                i += 1  # 消费程序值
                program_seen = True
            elif opt_name in data_opts:
                skip_next = True
            if is_git and opt_name.startswith(_GIT_FORBIDDEN_LONG):
                return (
                    f"git option {opt_name!r} is not allowed: it would point git at "
                    "a repository outside the project"
                )
            if is_git and git_subcommand == "config" and opt_name in _GIT_CONFIG_FORBIDDEN_SCOPES:
                return f"git config {opt_name} writes outside the project; use repo-local config"
            if is_python and not opt_name.startswith("--"):
                # -S/-I/-E（含 -IS 之类组合）会跳过 sitecustomize 或忽略
                # PYTHONPATH，解除运行时文件守卫。
                if set(opt_name[1:]) & set("SIE"):
                    return (
                        f"{cmd_base} with -S/-I/-E is not allowed: it would bypass "
                        "the Python runtime file guard"
                    )
            if is_node and opt_name in _NODE_EVAL_OPTS:
                return (
                    f"node {opt_name} is not allowed; "
                    "write a script file and run it instead"
                )
            i += 1
            continue

        # --- 位置 token ---
        if is_git and git_subcommand is None:
            git_subcommand = tok
            reason = _check_git_subcommand(tok)
            if reason:
                return reason
            i += 1
            continue
        if is_git and git_subcommand == "config":
            # `git config key value` 会写入 .git/config——可执行键（core.fsmonitor
            # 等）在后续命令（含 git_repo 工具自己的 status 检查）上触发；
            # `git -c` 检查拒绝它们，写入路径也必须拒绝。其余值必须像 -c 检查
            # 要求的那样是惰性字面量。读取/删除形式（`--get/--unset/
            # --remove-section key`）不带值。
            if git_config_key is None:
                git_config_key = tok
            else:
                if _GIT_CONFIG_EXECUTABLE_KEY_RE.match(git_config_key):
                    return (
                        f"git config {git_config_key!r} is not allowed: git "
                        "executes this key on later commands; keep the "
                        "setting in a repo file instead"
                    )
                if not _is_safe_git_c_value(f"{git_config_key}={tok}"):
                    return (
                        f"git config {git_config_key!r} value {tok!r} must "
                        "be a plain literal; keep it in a repo file instead"
                    )
            i += 1
            continue
        if is_python or is_node:
            # 第一个位置参数是脚本文件——作为路径校验。它之后的一切是脚本自己的
            # argv（运行时守卫约束实际文件访问）——但重定向除外：bash 在子进程
            # 运行前自己执行它们，因此运行时守卫看不到那些文件打开。
            # `python script.py > ../sibling/out` 曾无人阻拦地写到项目之外。
            reason = _check_path(tok, cwd=cwd, root=root,
                                 allow_dollar=allow_substitution,
                                 assigned_vars=assigned_vars, tainted_vars=tainted_vars)
            if reason:
                return reason
            # `python3 -m pip install` 会改动解释器的 site-packages；
            # --dry-run 是被接受的依赖解析渠道（pip-audit 内部正是调用它），
            # 且不写入任何东西。模块之后的 argv 是脚本自己的，因此 pip 子命令
            # 是这里唯一值得检查的东西。
            if is_python and (pip_seen or tok == "pip"):
                # `-m pip` 时模块已被 skip_next 消费，因此第一个位置参数就是
                # 子命令；没有 `-m` 时，`pip` 是位置参数，子命令跟在它后面。
                rest = tokens[i + 1:] if tok == "pip" else tokens[i:]
                first_word = next(
                    (t for t in rest if not t.startswith("-") and not _ASSIGN_RE.match(t)),
                    None,
                )
                if first_word == "install" and "--dry-run" not in rest:
                    return (
                        "pip install is not allowed: it mutates the Python "
                        "environment; use 'python3 -m pip install --dry-run' "
                        "for dependency resolution or python3 -m pip_audit "
                        "for CVE scans"
                    )
            j = i + 1
            while j < len(tokens):
                rest = tokens[j]
                if rest in _REDIRECT_PATH_OPS:
                    if j + 1 >= len(tokens):
                        return f"Redirection {rest!r} without a target"
                    reason = _check_redirect(
                        tokens[j + 1], cwd=cwd, root=root, temp=temp,
                        allow_dollar=allow_substitution,
                        assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                    )
                    if reason:
                        return reason
                    j += 1
                j += 1
            return None
        if cmd_base in _DATA_COMMANDS:
            # 数据参数是惰性文本——但命令替换除外，bash 会真实执行它们。
            # echo "$(cat /etc/passwd)" 曾跳过所有检查（allow_substitution=True），
            # 无人阻拦地读取主机文件。替换载荷由 validate_command 顶部的
            # _check_all_substitutions 针对整条命令、在原始文本上校验，因此单引号
            # （字面量）和转义的 `\$(` 都被尊重。这里不重复检查：shlex 拼接后的
            # token 流已经丢失引号信息（`echo '$(cat /etc/passwd)'` 重新拼接时没有
            # 引号，会被误判为真实替换）。
            i += 1
            continue
        if cmd_base in _FIRST_POS_DATA and not first_pos_seen:
            first_pos_seen = True
            if is_awk_sed:
                if program_seen:
                    # 程序来自 -e/--expression/-f/--file——这个位置参数是文件
                    # 操作数，必须通过路径检查（如 `sed -e 's/^/x/' /etc/passwd`）。
                    # -i 模式下它被就地改写，因此 .git 也禁止指向。
                    reason = _check_path(tok, cwd=cwd, root=root,
                                         allow_dollar=allow_substitution,
                                         assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                                         deny_git=in_place)
                else:
                    # 程序字符串被路径检查跳过——改为扫描其中的逃逸原语
                    # （awk system()/getline、sed e）。
                    reason = _check_program_string(tok, cmd_base)
            elif pattern_seen:
                # 模式/程序来自选项（-e/-f/--regexp/...）——这个位置参数是文件
                # 操作数，必须通过路径检查（`grep -e foo ../sibling/secret`）。
                reason = _check_path(tok, cwd=cwd, root=root,
                                     allow_dollar=allow_substitution,
                                     assigned_vars=assigned_vars, tainted_vars=tainted_vars)
            if reason:
                return reason
            i += 1
            continue
        reason = _check_path(tok, cwd=cwd, root=root,
                             allow_dollar=allow_substitution,
                             assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                             deny_git=(
                                 cmd_base in _GIT_WRITE_OPERAND_COMMANDS
                                 or (is_awk_sed and in_place)
                             ))
        if reason:
            return reason
        i += 1

    return None


def _check_git_subcommand(subcommand: str) -> str | None:
    if subcommand not in _GIT_SUBCOMMANDS:
        return (
            f"git subcommand {subcommand!r} is not allowed. "
            f"Supported: {sorted(_GIT_SUBCOMMANDS)}"
        )
    return None


def _git_internal_path(tok: str) -> str | None:
    """当 *tok* 指向 ``.git/`` 内任何东西时，返回原因字符串。

    ``.git/hooks/*`` 安装的钩子，git 在后续命令上不经校验地执行（经 git 自身
    触发的 ./script 逃逸）；``.git/config`` 可把 ``hooksPath`` 重指向任意目录；
    嵌套仓库把钩子藏在 ``subdir/.git/hooks/``，子模块藏在
    ``.git/modules/<name>/hooks/``。.git 由 git 自己管理——没有项目操作需要写入
    那里。
    """
    rel = tok.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    # 不区分大小写：在 Windows/macOS 文件系统上 `.GIT/hooks/x` 就是
    # `.git/hooks/x`，因此精确匹配检查会让守卫被大小写绕过（git 自己在那里
    # 不区分大小写地解析目录）。
    if any(part.lower() == ".git" for part in rel.split("/")):
        return (
            f"Path {tok!r} writes inside .git — hooks and config are git "
            "internals that execute unvalidated code or corrupt the repo; "
            "keep scripts in the project and run them explicitly"
        )
    return None


def _check_redirect(
    tok: str, *, cwd: Path, root: Path, temp: Path, allow_dollar: bool,
    assigned_vars: set[str], tainted_vars: frozenset[str] | None = None,
) -> str | None:
    """重定向目标必须留在项目内或系统临时目录。"""
    reason = _git_internal_path(tok)
    if reason:
        return reason
    reason = _check_path(tok, cwd=cwd, root=root, allow_dollar=allow_dollar,
                         assigned_vars=assigned_vars, tainted_vars=tainted_vars)
    if reason is None:
        return None
    # 系统临时目录内的绝对路径（如 > /tmp/sorted.txt）没问题——临时目录是临时
    # 的，且本就是 Python 运行时守卫的写入根。
    if tok.startswith("/") or _DRIVE_RE.match(tok):
        try:
            if is_within(temp, Path(tok).resolve()):
                return None
        except OSError:
            pass
    return reason


def _has_param_expansion(value: str) -> bool:
    """当 *value* 使用了超出纯名字的 ${...} 时为真。

    `${x:-/etc}` / `${x:=y}` / `${x:+y}` / `${x%a}` / `${x#b}` 会展开成后续
    路径位置会原样使用的值——`${x:-/etc}` 让 `cat $x/passwd` 成为绝对路径。
    普通引用（`$x`、`${x}`）仍可追溯：其值在赋值处已校验。
    """
    for m in re.finditer(r"\$\{([^}]*)\}", value):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            return True
    return False


# 会改变命令解析或执行方式的环境变量：对其中一个赋值，会让命令的参数变成实际
# 运行的代码（PATH=. python 执行 ./python；GIT_EXTERNAL_DIFF='cat /etc/passwd'
# 在每次 diff 时运行；LD_PRELOAD 加载任意共享对象；GIT_DIR 重指向仓库）。它们
# 的值永远不是合法项目数据——按名字拒绝赋值，无论值是什么。IFS 也属于此：
# 它改变每个未引号展开的词边界，因此 `IFS=:; x=a:/etc/passwd` 在运行时把一个
# 已校验单词变成绝对路径。
_EXECUTION_ENV_VARS = frozenset({
    "PATH", "CDPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV",
    "SHELLOPTS", "BASHOPTS", "IFS",
    "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_PAGER",
    "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    # 解释器/虚拟机控制变量：node/java/python 是白名单命令，而这些让解释器在
    # 每次运行时加载攻击者选择的代码（--require / -javaagent / 伪造标准库），
    # 无需额外标志。命令前缀通道和 shell env_refs 都查阅此集合。
    "NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    "PYTHONHOME", "PYTHONSTARTUP",
})

# GIT_CONFIG_COUNT 加上成对的 GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n，像
# `git -c` 一样注入任意 git 配置：diff.external、core.sshCommand、core.pager 和
# 过滤驱动都会执行命令选择的值。这些带索引的名字无法在上面的集合中枚举。
_GIT_CONFIG_ENV_RE = re.compile(r"^GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+)$")


def _check_assignment(tok: str, *, allow_dollar: bool,
                      assigned_vars: set[str] | None = None) -> str | None:
    """校验开头 ``VAR=value`` 赋值的值。

    该值可能在后续参数中经 ``$VAR`` 展开，因此即使赋值本身不触碰任何文件，
    藏在值里的逃逸路径（``p=../proj-b``）也必须拒绝。``allow_dollar`` 为
    code_executor 的 bash 模式放宽检查，那里的环境变量（OUTPUT_DIR、
    PROJECT_DIR、HOME）都指向项目内——但参数展开（${x:-...}）仍可展开为任何
    东西，会被拒绝。
    """
    name, eq, value = tok.partition("=")
    if name in _EXECUTION_ENV_VARS or _GIT_CONFIG_ENV_RE.match(name):
        return (
            f"Assignment to {name!r} is not allowed: it changes how commands "
            "are resolved or executed (path hijack, external diff/editor, "
            "repo redirection); use allowlisted commands with literal paths"
        )
    if not eq:
        return None
    if not allow_dollar and "$" in value:
        return (
            f"Assigned value {value!r} contains variable expansion, which "
            "cannot be validated; assign a literal path instead"
        )
    if allow_dollar and _has_param_expansion(value):
        return (
            f"Assigned value {value!r} contains a parameter expansion "
            "(${{x:-...}}) whose result could be any path; assign a literal "
            "or a plain variable reference instead"
        )
    if allow_dollar:
        # 普通 `$VAR` 引用必须可追溯：主机环境变量（$SHELL、$PATH）展开为
        # 主机路径，未设置的变量展开为空——`x=$UNSET; cat $x/etc/hostname`
        # 塌缩成绝对路径，即以非空值复活的空赋值逃逸。只有本命令中更早赋值
        # （在 code_executor 中是更早行）的名字，以及服务器注入的项目内变量
        # 才合格。
        for ref in _dollar_vars(value):
            if ref not in (assigned_vars or ()) and ref not in _SAFE_ENV_PATH_VARS:
                return (
                    f"Assigned value {value!r} references {ref!r}, which is "
                    "neither assigned earlier nor a project-internal env "
                    "var; assign a literal or an earlier-assigned name "
                    "instead"
                )
    reason = _check_assignment_fields(value)
    if reason:
        return reason
    return _check_simple(value)


def _check_assignment_fields(value: str) -> str | None:
    """校验未加引号的 ``$name`` 展开会产生的每个单词。

    变量未加引号展开时，bash 按 IFS 空白做字段拆分，因此 ``x=" /etc/passwd";
    cat $x`` 被校验为一个相对单词，却执行 ``cat /etc/passwd``。每个字段因此
    必须通过独立单词检查，而以 ``-`` 开头的字段可能被拼到选项位置
    （``x=--output=/etc/passwd; sort $x``）。结尾空白单独拒绝：
    ``x="dir "; cat $x/etc/passwd`` 会把附加的字面量拆成第二个绝对单词。
    """
    if not value:
        return None
    if value != value.rstrip():
        return (
            f"Assigned value {value!r} ends with whitespace; a later "
            "``$VAR/etc``-style expansion would split the appended path off "
            "as a separate word; remove the trailing whitespace"
        )
    fields = value.split()
    if not fields:
        return (
            f"Assigned value {value!r} is whitespace-only; an unquoted "
            "expansion of it vanishes, which can collapse ``$VAR/path`` "
            "into an absolute path"
        )
    for field in fields:
        if field in ("-", "--"):
            continue
        if field.startswith("-"):
            return (
                f"Assigned value {value!r} contains the option-like word "
                f"{field!r}; a later unquoted $VAR expansion could place it "
                "in an option position, so assign plain relative paths only"
            )
        reason = _check_simple(field)
        if reason:
            return (
                f"Assigned value {value!r} expands to {field!r} after field "
                f"splitting, which is unsafe: {reason}"
            )
    return None


def _check_simple(value: str) -> str | None:
    """绝对/波浪路径和 '..' 段一律不允许。"""
    if value in ("", "-", ".", "/dev/null"):
        return None
    if value.startswith("/") or value.startswith("~"):
        return (
            f"Absolute path {value!r} is not allowed. "
            "Use paths relative to the project directory."
        )
    if any(seg == ".." for seg in _SEP_RE.split(value)):
        return f"'..' path segments are not allowed (in {value!r}); stay inside the project directory"
    return None


# 独立值选项是程序字符串、需扫描逃逸的 awk/gawk/sed 命令（sed -e '...'、
# gawk -e/--source '...'）。awk 自己的程序永远只是第一个位置参数。
_EXPRESSION_VALUE_OPTS: dict[str, frozenset[str]] = {
    "gawk": frozenset({"-e", "--source"}),
    "sed": frozenset({"-e", "--expression"}),
}

# awk/gawk/sed 程序字符串内的逃逸原语。程序字符串被 token 级路径检查跳过
# （它是数据，不是路径），因此该扫描是模式与 awk system()/getline 或 sed
# 的 `e` 之间唯一的关卡——它们每个都会执行任意 shell 命令或读写项目之外的
# 文件。刻意收窄，让合法程序（{print $1}、s/foo/bar/g）通过。扫描前先遮蔽
# 双引号段，使 Agent 可能合法打印的字符串字面量绝不触发模式。
_AWK_PROGRAM_ESCAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsystem\s*\("),   # 执行 shell 命令
    re.compile(r"\bpopen\s*\("),
    re.compile(r"\|&"),             # 双向管道——派生进程
    re.compile(r"\|\s*\""),         # 通向命令字符串的单向管道——shell
    re.compile(r"\bgetline\b"),     # 经 < "path" 读取任意文件
)
_SED_PROGRAM_ESCAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsystem\s*\("),
    re.compile(r"\bpopen\s*\("),
    # sed 的独立 `e` 命令——把行剩余部分交给 shell。前一个字符也可能是地址
    # 分隔符（`/./e`、`\#HOST#e` 用反斜杠转义分隔符作地址、`!` 是取反地址）。
    # 命令字母必须跟在地址/分隔符字符之后，可跨空白（`1 e`）；`e` 前一个普通
    # 空格并不够——`a\hello e` 之类文本命令把该字母嵌在字面文本里。`e` 之后
    # 不做尾部约束：`1ewhoami` 和 `ecat /etc/passwd` 原样执行行剩余部分。
    # `\n` 加入前面的字符类，使多行脚本中行首的 `e`（`s/x/y/\ne cat ...`）
    # 也命中；这允许的罕见误报（以 `e` 开头的 `a\` 续接文本）被接受——方向是
    # 失败时关闭。
    re.compile(r"(?:^|[$0-9,;{}/\\\n!]\s*)e"),
    # 反斜杠转义分隔符地址（`\#HOST#e`）——用同一分隔符 \x ... x 同时开启和
    # 关闭地址；命令可由空白分隔（`\#HOST# eid`）。也不做尾部约束
    # （`\#HOST#ecat /etc/passwd` 运行 `cat /etc/passwd`）。
    re.compile(r"\\(.)[^\\\n]*\1\s*e"),
    # `:label e`——标签定义后接 `e` 命令；标签是单个单词，`e` 跟在空白之后。
    # 罕见误报（含 ` e` 的标签文本）被接受，失败时关闭。
    re.compile(r"(?:^|[;:\s])[A-Za-z_][A-Za-z0-9_]*[ \t]+e"),
    # s///e / y///e 标志——GNU sed 经 shell 执行替换。分隔符是任意非空白字符
    # （GNU sed 没有固定分隔符集合），且 `e` 是标志，除非它位于 `w` 粘连的
    # 文件名里（`s/x/y/we` 写文件 `e`，`s/x/y/ew` 仍运行 shell）。s/y 前缀
    # 阻止文本命令载荷（`c\done`、`a\hello`）被当作替换命令扫描。尾部字符类
    # 接纳计数形式（`s|x|y|e2` 仍经 shell 执行替换）。
    re.compile(r"(?:s|y)([^\\\n\s])(?:\\.|[^\\\n])*?\1(?:\\.|[^\\\n])*?\1(?:(?!w)[A-Za-z])*?e(?=(?:(?!w)[A-Za-z0-9])*$|w|[; \t}\n0-9]|$)"),
)

# sed r/w/R/W 命令上粘连的文件目标（`1r/etc/passwd`、`$w out.txt`）——文件名
# 粘在命令 token 上，整个 token 的 _check_simple 永远看不到它（已验证 GNU sed
# 会读取粘连目标）。像其他路径 token 一样提取并校验。
_SED_ATTACHED_TARGET_RE = re.compile(r"(?:^|[;}\s])(?:\d+|\$)?[rwRW]\s*([^;\s}\n]+)")
# 数字地址粘连形式（`1,10w/tmp/x`、`2,5r/etc/passwd`）——地址数字/逗号位于
# 命令字母之前，上面的模式（仅单数字或 $）无法消费。
_SED_ADDR_TARGET_RE = re.compile(r"(?:^|[;}\s])(?:\d+(?:,\d+)?|\$)[rwRW]\s*([^;\s}\n]+)")
# 文件名粘连的 s///w / s///gw 标志（`s/x/y/w/tmp/out`）——GNU sed 把 w 标志
# 之后的一切当作文件名。分隔符是任意非空白字符（匹配上面的 s///e 模式）。
_SED_SFLAG_W_TARGET_RE = re.compile(r"s([^\\\n\s])[^\\\n]*?\1[^\\\n]*?\1(?:[A-Za-z]*w)\s*([^;\s}\n]*)")


def _check_program_string(program: str, cmd: str) -> str | None:
    """扫描 awk/gawk/sed 程序字符串中的沙箱逃逸原语。

    token 级路径检查会跳过这些引号字符串，否则 ``awk 'system("curl ...")'``
    或 ``sed '1e'`` 之类模式会逃逸沙箱。同时用与 token 检查相同的绝对/波浪/
    点号规则，检查程序内嵌的文件名（awk 字符串字面量、sed 裸 r/w 目标）。
    """
    masked = re.sub(r'"(?:[^"\\]|\\.)*"', '""', program)
    patterns = _SED_PROGRAM_ESCAPES if cmd == "sed" else _AWK_PROGRAM_ESCAPES
    for pattern in patterns:
        if pattern.search(masked):
            return f"{cmd} program {program!r} contains an escape primitive ({pattern.pattern!r}); not allowed"
    # 程序内的文件名：awk 双引号字符串、sed 裸 r/w 目标——像 token 检查一样拒绝
    # 绝对/波浪/点号路径。
    if cmd == "sed":
        try:
            lexer = shlex.shlex(program, posix=True)
            lexer.whitespace_split = True
            # sed 没有 `#` 注释语法（与 sh/awk 不同）——`#` 是合法的替换分隔符
            # （`s#a#b#w/tmp/x`），因此这里不能剥离它，否则粘连的 w 目标永远
            # 提取不出来。
            lexer.commenters = ""
            candidates = list(lexer)
        except ValueError:
            candidates = program.split()
        for tok in candidates[1:]:
            reason = _git_internal_path(tok)
            if reason:
                return f"{cmd} program {program!r}: {reason}"
            reason = _check_simple(tok)
            if reason:
                return f"{cmd} program {program!r}: {reason}"
        # 粘连的 r/w/R/W 目标可粘在任何程序 token 上——包括第一个（`1r/etc/passwd`），
        # 上面的 [1:] 切片刻意跳过它（`/^$/d` 之类开头的地址正则不能被当作绝对
        # 路径拒绝）。
        for tok in candidates:
            for pattern in (_SED_ATTACHED_TARGET_RE, _SED_ADDR_TARGET_RE, _SED_SFLAG_W_TARGET_RE):
                for target in pattern.findall(tok):
                    # _SED_SFLAG_W_TARGET_RE 除文件名外还捕获了分隔符（用于其
                    # \1 反向引用）——取最后一个分组。
                    if isinstance(target, tuple):
                        target = target[-1]
                    # r/w/R/W 目标是程序内嵌的文件名；w 目标会写入，经 sed 读取
                    # 很罕见，因此 .git 对两者都失败时关闭。
                    reason = _git_internal_path(target)
                    if reason:
                        return f"{cmd} program {program!r}: {reason}"
                    reason = _check_simple(target)
                    if reason:
                        return f"{cmd} program {program!r}: {reason}"
    else:
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', program):
            # awk 字符串字面量可能是重定向目标（`print > ".git/config"`），
            # 因此这里也禁止指向 .git。
            reason = _git_internal_path(m.group(1))
            if reason:
                return f"{cmd} program {program!r}: {reason}"
            reason = _check_simple(m.group(1))
            if reason:
                return f"{cmd} program {program!r}: {reason}"
    return None


_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# 由服务器注入、值保证为项目内路径的环境变量（code_executor 的 safe_env 设置
# HOME/OUTPUT_DIR/PROJECT_DIR；PWD 是 bash 自己的，锚定在项目 cwd）。这些是
# 路径位置唯一允许的主机环境变量——$SHELL/$PATH/$USER 等在运行时可展开为任何
# 东西。
_SAFE_ENV_PATH_VARS = frozenset({"HOME", "OUTPUT_DIR", "PROJECT_DIR", "PWD"})


def _dollar_vars(tok: str) -> list[str]:
    """返回 *tok* 中 $var / ${var} 展开引用的变量名。"""
    return [m.group(1) or m.group(2) for m in _DOLLAR_RE.finditer(tok)]


def _check_path(
    tok: str, *, cwd: Path, root: Path, allow_dollar: bool, assigned_vars: set[str],
    tainted_vars: frozenset[str] | None = None, deny_git: bool = False,
) -> str | None:
    # deny_git 只在写入位置启用（cp/mv/mkdir/touch 操作数、sed -i / gawk -i
    # inplace 文件操作数、sort -o 值）：git 之后会执行的 .git 内文件（钩子、
    # 带 hooksPath 的配置）绝不能来自这些命令。读取仍允许，与文件系统工具一致
    # ——`cat .git/HEAD` 无害。
    if deny_git:
        reason = _git_internal_path(tok)
        if reason:
            return reason
    reason = _check_simple(tok)
    if reason:
        return reason
    # 非路径伪参数（stdin 标记、当前目录、空设备）。
    if tok in ("-", ".", "/dev/null"):
        return None
    # 路径位置的命令替换在运行时会真实执行——`cat "$(echo /etc/passwd)"` 把
    # 载荷藏在静态检查之外（内部命令是没有开头 / 的数据命令）。直接拒绝；数据
    # 命令（`echo "$(date)"`）永远到不了 _check_path。反引号对 shlex 是普通
    # 字符（不是引号），因此原始的 `\`x\`` token 原样出现。
    if "$(" in tok or "`" in tok or tok == "$" or tok.endswith("/$"):
        # `cat $(echo /etc/passwd)` 作为裸 `$` token 到达这里（shlex 把 `$(`
        # 拆成 `$` + `(`），`cat dir/$(echo x)` 作为 `dir/$`——与上面引号形式
        # 相同的路径内替换漏洞。字面 `file$`（名字以 $ 结尾的文件）仍允许：
        # 只有裸的或目录粘连的 `$` 才是无歧义的替换。
        return f"Command substitution is not allowed in a path argument: {tok!r}"
    # 位置参数（$1、${2}、$@、$*、$#）从不被 code_executor 设置——它们在运行时
    # 展开为空，因此 `cat $1/etc/passwd` 会在下方静态检查之外静默变成
    # `cat /etc/passwd`（绝对路径）。在路径位置直接拒绝它们。
    if re.search(r"\$\{[0-9]+\}|\$[0-9]|\$[@*#]", tok):
        return (
            f"Path {tok!r} uses a positional parameter, which is never set "
            "here (it would expand empty); use a literal path inside the project"
        )
    # 值从命令替换捕获的变量（`x=$(echo /etc/passwd); cat $x`），在运行时可
    # 解析为任何东西——无论 allow_dollar 如何，在路径位置都拒绝。
    if tainted_vars:
        used = set(_dollar_vars(tok))
        if used & tainted_vars:
            return (
                f"Path {tok!r} expands a variable captured from command "
                "substitution; use a literal in-project path instead"
            )
    if allow_dollar:
        # `${Y:-/etc}` / `${Y:=x}` / `${Y%pat}` / `${Y#pat}` /
        # `${Y/old/new}` 在运行时可展开为任何东西——`cat ${Y:-/etc}/passwd`
        # 变成 `cat /etc/passwd`，而 _dollar_vars 看不到该变量（其正则要求
        # `}` 紧跟名字之后）。在路径位置直接拒绝参数展开。
        if _has_param_expansion(tok):
            return (
                f"Path {tok!r} uses a parameter expansion (${{x:-...}} etc.), "
                "whose result could be any path; use a literal path inside "
                "the project"
            )
        # allow_dollar 只在已知变量值会留在项目内时，才信任路径位置的 $var：
        # 在本命令中赋值（赋值处已校验），或属于值为项目内路径的服务器注入
        # 环境变量（HOME、OUTPUT_DIR、PROJECT_DIR、PWD）。主机环境变量
        # （$SHELL、$PATH、$USER）在运行时可展开为任何东西——`rm $SHELL`
        # 会删除项目外文件，`cat $PATH` 会读取一个。
        allowed = assigned_vars | _SAFE_ENV_PATH_VARS
        vars_used = _dollar_vars(tok)
        if vars_used and not all(v in allowed for v in vars_used):
            return (
                f"Path {tok!r} uses variable {vars_used!r} that is not assigned "
                "in this command or a project-internal env var; assign it first "
                "or use a literal path inside the project"
            )
        # `$var/` 或 `${var}/` 前缀在变量为空时塌缩成绝对路径（y 未设置时
        # `cat $y/etc/passwd` 运行 `cat /etc/passwd`）——前缀变量同样必须已赋值
        # 或为项目内环境变量（注入的那些始终非空）。
        prefix_names = [
            m.group(1) or m.group(2)
            for m in re.finditer(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}/|\$([A-Za-z_][A-Za-z0-9_]*)/", tok
            )
        ]
        if prefix_names and not all(n in allowed for n in prefix_names):
            return (
                f"Path {tok!r} uses variable {prefix_names!r} as a path prefix "
                "but it is not assigned in this command; assign it first or "
                "use a literal path inside the project"
            )
    if not allow_dollar and "$" in tok:
        # 只有本命令字符串中更早赋值的变量可以出现在路径参数中——它们的值在
        # 赋值处已校验。主机环境变量（$HOME、$PATH）或位置标记（$1、$@）可
        # 展开为任何东西，会使 token 级路径检查失效。
        vars_used = _dollar_vars(tok)
        if not vars_used or not all(v in assigned_vars for v in vars_used):
            return (
                f"Path {tok!r} uses a variable that is not assigned in this "
                "command; assign it first (VAR=value) or use a literal path "
                "inside the project"
            )
    # 裸文件名也会被解析。项目内的符号链接（git 检出会物化已提交的符号链接；
    # `x -> /etc/passwd` 或 `x -> ../../sibling/.env`）会让裸 `cat x` 读到
    # 项目之外——resolve() 跟随链接，因此 is_within 检查能捕获它。
    try:
        resolved = (cwd / tok).resolve()
    except OSError:
        return f"Path {tok!r} could not be resolved; refusing to run"
    if not is_within(root, resolved):
        return f"Path {tok!r} escapes the project directory"
    return None
