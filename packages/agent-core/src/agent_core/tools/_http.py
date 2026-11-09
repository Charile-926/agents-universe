"""智能体工具共用的 HTTP 客户端工厂。

根据环境配置代理、SSL、超时与 SSRF 防护。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from .base import ToolContext
from ._ssrf import SSRFError, resolve_and_validate, validate_url, validate_redirect

_DEFAULT_TIMEOUT = 60.0


def _header_value_problem(value: str) -> str | None:
    """*value* 为何不能作为 HTTP 头值发送，能发送则返回 None。

    httpx/h11 拒绝非法值时会把它原样回显（"Illegal header value b'...'"）——对
    渲染出的密钥而言，该消息本身就是凭证，会进入连接警告、工具错误结果和测试端点
    响应（format_exc 风格的链也会携带它）。在传输层看到该值之前先校验；调用方的
    错误只点名头字段。非 ASCII 本来就会失败（httpx 把 str 值按 ascii 编码），其
    UnicodeEncodeError 会引用出错字符。
    """
    if not value.isascii():
        return "contains a non-ASCII character"
    if any(ch != "\t" and ord(ch) < 0x20 for ch in value):
        return "contains a control character (a pasted trailing newline, perhaps)"
    # h11 拒绝首尾 OWS 并在错误中回显整个值——对渲染出的密钥，该消息本身就是
    # 凭证。非空白段之间的内部空格/制表符合法。
    if value != value.strip(" \t"):
        return "has leading or trailing whitespace (a pasted trailing space, perhaps)"
    return None


def redact_secret(text: str, secret: str | None) -> str:
    """把 *text* 中的 *secret* 替换为 ``[REDACTED]``。

    同时匹配原始值及其字节 repr 的转义形式：h11 和回溯通过 repr 引用值，因此粘贴
    带来的尾随换行在那里以字面反斜杠-n 出现，原始子串替换会漏掉。规则与
    token/api-key 路由中的 ``_redact_key`` 辅助函数相同。
    """
    if not text or not secret:
        return text
    text = text.replace(secret, "[REDACTED]")
    try:
        escaped = repr(secret.encode("utf-8"))[2:-1]
    except Exception:
        return text
    if escaped and escaped != secret:
        text = text.replace(escaped, "[REDACTED]")
    return text


def _should_bypass_proxy(target_url: str, no_proxy: str) -> bool:
    """检查 target_url 是否匹配 NO_PROXY 列表中的任意条目。"""
    if not no_proxy or not target_url:
        return False
    host = urlparse(target_url).hostname or ""
    if not host:
        return False
    for pattern in no_proxy.split(","):
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if host == pattern or host.endswith("." + pattern.lstrip(".")):
            return True
    return False


def _is_ssrf_enabled() -> bool:
    """SSRF 防护默认关闭。设置 SSRF_ENABLED=true 开启。"""
    return os.environ.get("SSRF_ENABLED", "").lower() in ("1", "true", "yes")


def validate_outbound_url(url: str) -> None:
    """在发出出站请求前校验 URL 的 SSRF 安全性。

    SSRF 防护默认禁用，通过环境变量 SSRF_ENABLED=true 开启。开启后检查协议、端口，
    并解析 DNS 以确认目标 IP 不在封禁段（私有、环回、链路本地、元数据等）。

    URL 不安全时抛出 SSRFError。
    """
    if not _is_ssrf_enabled():
        return
    validate_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolve_and_validate(hostname, port)


class _SSRFPinningTransport(httpx.AsyncBaseTransport):
    """消除面向智能体请求上的 DNS 重绑定 TOCTOU 时间窗。

    工具在请求“之前”校验 URL（含 DNS），但 httpx 在连接时会重新解析主机名——重绑定
    DNS 服务器可对校验查询回答公网 IP，对连接查询回答 127.0.0.1。该传输在请求时
    重新解析并把连接固定到已校验的 IP：URL 主机改写为 IP，同时 Host 头和 TLS SNI
    （httpcore 的 sni_hostname 扩展）保留原名，使虚拟主机和证书校验行为正常。
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def aclose(self) -> None:
        # AsyncBaseTransport.aclose 是空操作；不重写它，关闭客户端会让内部传输的
        # 连接池一直开着。
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _is_ssrf_enabled():
            await self._pin(request)
        return await self._inner.handle_async_request(request)

    async def _pin(self, request: httpx.Request) -> None:
        import asyncio
        import ipaddress

        url = request.url
        host = url.host
        if not host:
            return
        # 字面 IP 已由调用处的 validate_url 校验过；端口/协议检查同样在请求前完成
        # （多个调用处合法使用非白名单端口，因此这里不重复检查）。
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        port = url.port or (443 if url.scheme == "https" else 80)
        # getaddrinfo 是阻塞的——让它远离事件循环。
        safe_ips = await asyncio.to_thread(resolve_and_validate, host, port)
        ip = safe_ips[0]
        # Request.url 是普通属性——在内部传输看到请求前重赋它即可重定向连接；代理
        # CONNECT 目标和 Host 头都派生自这里的设置。
        # copy_with(host=...) 会给 IPv6 加括号并保留 URL 自身端口；
        # copy_with(netloc=...) 无法解析带括号的 IPv6。
        request.url = url.copy_with(host=ip)
        request.headers["Host"] = host if (port == 80 or port == 443) else f"{host}:{port}"
        request.extensions["sni_hostname"] = host
        # 绝不复用固定连接：httpcore 按改写后的源（协议、IP、端口）做池化，因此之后
        # 对解析到同一 IP 的另一主机名的请求会复用这条连接——其 TLS 会话只针对第一
        # 个主机名校验过，且其头（Authorization！）会到达控制该 IP 的人。每次响应后
        # 关闭，强制重新握手并针对当前主机名校验证书。
        request.headers["Connection"] = "close"


def _ssrf_transport_kwargs(ssl_verify: bool, proxy: str | None) -> dict:
    """ensure_http_client 各客户端的传输参数。

    SSRF 开启时，把真实传输包进固定层，并把代理移到内部传输上（传入自定义传输时
    httpx 会忽略 proxy=）。关闭时——代理作为普通客户端参数。
    """
    if not _is_ssrf_enabled():
        return {"proxy": proxy} if proxy else {}
    return {
        "transport": _SSRFPinningTransport(
            httpx.AsyncHTTPTransport(verify=ssl_verify, proxy=proxy)
        )
    }


def ensure_http_client(context: ToolContext, target_url: str = "") -> httpx.AsyncClient:
    """返回上下文中共享的 httpx 客户端，需要时创建一个。

    采用宽裕的默认超时，因为各类工具（Kong、Confluence）可能需要超过 30 秒。需要时
    单个请求可用 httpx 的逐请求超时覆盖。

    若 target_url 匹配 NO_PROXY，使用独立的非代理客户端。

    注意：这里返回共享客户端。调用方在对不可信 URL 发请求“之前”应使用
    validate_outbound_url()。内部/可信 URL（LLM 供应方、已配置的集成）可跳过 SSRF
    校验。
    """
    no_proxy = context.cfg("NO_PROXY") or os.environ.get("no_proxy", "")
    bypass = _should_bypass_proxy(target_url, no_proxy)

    ssl_verify = context.ssl_verify

    # 惰性资源位于共享上下文（copy_for_task 的属主引用）：任务克隆必须复用会话的
    # 客户端，否则每个并行任务都会打开自己的、cleanup() 永远看不到的连接池。
    owner = getattr(context, "_shared", None) or context

    if bypass:
        if owner.http_client_no_proxy is not None:
            context.http_client_no_proxy = owner.http_client_no_proxy
            return owner.http_client_no_proxy
        # SSRF 开启时固定传输包裹真实传输——自定义传输旁 httpx 忽略 proxy=，因此
        # 代理必须放在内部传输上（这里没有——该客户端明确不走代理）。
        owner.http_client_no_proxy = httpx.AsyncClient(
            verify=ssl_verify,
            timeout=_DEFAULT_TIMEOUT,
            trust_env=False,
            max_redirects=0,
            **_ssrf_transport_kwargs(ssl_verify, None),
        )
        context.http_client_no_proxy = owner.http_client_no_proxy
        return owner.http_client_no_proxy

    if owner.http_client is not None:
        context.http_client = owner.http_client
        return owner.http_client

    proxy = context.proxy_url()
    owner.http_client = httpx.AsyncClient(
        verify=ssl_verify,
        timeout=_DEFAULT_TIMEOUT,
        max_redirects=0,
        **_ssrf_transport_kwargs(ssl_verify, proxy),
    )
    context.http_client = owner.http_client
    return owner.http_client
