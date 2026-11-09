"""git 系工具共享的 git 子进程运行器。

从 git_repo 抽出，使每个调用 git 的工具——包括与配置主机之外远程通信的工具
（skill_source）——都走同一条加固路径：经临时 askpass 助手注入凭证、超时即杀、
输出清洗/截断，以及剥离服务器自身凭证的环境。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .base import ToolContext, apply_proxy_env

_log = logging.getLogger(__name__)
_TIMEOUT_CLONE = 300
_TIMEOUT_PULL = 120
_TIMEOUT_DEFAULT = 30
_MAX_OUTPUT = 20_000


def _find_git() -> str | None:
    if found := shutil.which("git"):
        return found
    if sys.platform != "win32":
        return None
    roots = {
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData"),
    }
    for root in roots - {None}:
        for relative in ("Git/cmd/git.exe", "Git/bin/git.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


_GIT_BIN = _find_git()


def _dependency_missing(message: str) -> dict[str, Any]:
    return {"error": "dependency_missing", "dependency": "git", "message": message}


def _sanitize_output(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token and text else text


def _rmtree_force(path: Path) -> None:
    """删除目录树，win32 上先清除只读属性。

    Windows 上 git 的松散对象以只读属性创建，shutil.rmtree 随后对它们报
    WinError 5 失败（克隆回滚用 ignore_errors 掩盖了这点）。先在所有位置清除
    属性，使真实失败（如杀毒软件文件锁）仍能作为 remove_failed 浮现。
    """
    if sys.platform == "win32":
        for root, _dirs, files in os.walk(path):
            for name in files + _dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
                except OSError:
                    pass
    shutil.rmtree(path)


def _safe_git_env() -> dict[str, str]:
    """为 git 子进程剥离类凭证键后的 os.environ。

    Git 钩子（post-commit、pre-push 等）以该环境执行，因此服务器环境里的数据库
    密码 / API 密钥绝不能到达它们。对应 ToolContext.safe_env() 的过滤；
    GIT_ASKPASS_TOKEN 由调用方在过滤后重新加回。代理拼写随后（在 run_git 的
    默认路径里）规范化——git 的 libcurl 读 ALL_PROXY 并被空占位符卡住，正是
    apply_proxy_env 清洗的内容。
    """
    deny_suffixes = ToolContext._ENV_DENY_SUFFIXES
    deny_prefixes = ToolContext._ENV_DENY_PREFIXES
    deny_exact = ToolContext._ENV_DENY_EXACT
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in deny_exact:
            continue
        if upper.rsplit("_", 1)[-1] in deny_suffixes:
            continue
        if any(upper.startswith(p) for p in deny_prefixes):
            continue
        env[key] = value
    return env


async def run_git(
    args: list[str],
    cwd: str | Path,
    timeout: int = _TIMEOUT_DEFAULT,
    token: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """以一次性 askpass 助手注入凭证的方式运行 git。

    ``env`` 替换默认过滤后的 os.environ——需要额外加固变量的调用方传入自己的
    副本（skill_source 已把它的副本经 ToolContext.proxy_env 处理，因此调用方提供
    的 env 原样保留）。令牌从不放在命令行或 URL 里；它只存在于助手的环境中。
    """
    if not _GIT_BIN:
        return _dependency_missing("Git executable was not found")
    if env is None:
        env = apply_proxy_env(_safe_git_env())
    else:
        env = dict(env)
    askpass: Path | None = None
    # 没有它，对需要凭证的仓库做匿名克隆会在终端提示上永久阻塞而非失败。
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        suffix = ".cmd" if sys.platform == "win32" else ".sh"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, prefix="git-askpass-", delete=False,
                encoding="utf-8", newline="",
            ) as helper:
                askpass = Path(helper.name)
                if sys.platform == "win32":
                    helper.write(
                        '@echo off\r\necho %~1 | findstr /I "password" >nul\r\n'
                        'if not errorlevel 1 (echo %GIT_ASKPASS_TOKEN%) else (echo oauth2)\r\n'
                    )
                else:
                    helper.write(
                        '#!/bin/sh\ncase "$1" in\n'
                        '  *[Pp]assword*) printf \'%s\\n\' "$GIT_ASKPASS_TOKEN" ;;\n'
                        '  *) printf \'oauth2\\n\' ;;\nesac\n'
                    )
            if sys.platform != "win32":
                askpass.chmod(0o700)
        except OSError as exc:
            # delete=False 把文件留在磁盘，下方 finally 只包裹子进程块——在这里
            # 清理，否则每次失败的助手写入都会泄漏一个 git-askpass-* 临时文件。
            if askpass is not None:
                try:
                    askpass.unlink(missing_ok=True)
                except OSError:
                    pass
            return _dependency_missing(f"Could not create Git credential helper: {exc}")
        env.update(
            GIT_ASKPASS=str(askpass),
            GIT_ASKPASS_TOKEN=token,
        )
    # process 预先声明：取消若落在 create_subprocess_exec 的 await 与其赋值之间，
    # 否则会在下方 CancelledError 处理器里撞上 NameError（同 code_executor.py /
    # shell.py 的模式）。
    process: asyncio.subprocess.Process | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                _GIT_BIN, *args, cwd=str(cwd), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
                await process.wait()
            return {"error": f"Git command timed out after {timeout}s"}
        except asyncio.CancelledError:
            if process is not None:
                process.kill()
                await process.wait()
            raise
        except OSError as exc:
            return _dependency_missing(f"Git could not be started: {exc}")
        out = _sanitize_output(stdout.decode(errors="replace"), token)[:_MAX_OUTPUT]
        err = _sanitize_output(stderr.decode(errors="replace"), token)[:_MAX_OUTPUT]
        if process.returncode:
            _log.warning("git %s failed: %s", args[0] if args else "?", err[:500])
            return {
                "error": f"Git exited with code {process.returncode}",
                "exit_code": process.returncode,
                "stdout": out,
                "stderr": err,
            }
        return {"stdout": out, "stderr": err, "exit_code": 0}
    finally:
        if askpass:
            try:
                askpass.unlink(missing_ok=True)
            except OSError:
                pass
