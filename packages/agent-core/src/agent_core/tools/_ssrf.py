"""SSRF 防护——在发出出站请求前校验 URL 与解析出的 IP。

拦截：
- 非 http(s) 协议
- 私有/环回/链路本地/组播/元数据 IP 段（DNS 解析之后）
- 受限端口
- 过多重定向（每一跳都重新校验）
- 超大响应
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

# AWS/GCP/Azure 元数据端点
_METADATA_IPS = frozenset({
    "169.254.169.254",
    "fd00:ec2::254",
    "metadata.google.internal",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# 前端开发服务器（Vite 5173/4173/5174 等）是浏览器/web_fetch 工具的合法目标，
# 即使 SSRF_ENABLED=false 也应放行——不把它们放进白名单，智能体就永远打不开自己的
# 开发前端。
_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443, 8000, 3000, 5000, 9090, 5173, 4173, 5174})

_MAX_REDIRECTS = 5

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


class SSRFError(Exception):
    """URL 或解析出的 IP 未通过 SSRF 校验时抛出。"""
    pass


def _is_ip_blocked(ip_str: str) -> bool:
    """判断一个 IP 地址是否属于封禁段。"""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 无法解析 = 封禁

    if addr.is_loopback:
        return True
    if addr.is_private:
        return True
    if addr.is_link_local:
        return True
    if addr.is_multicast:
        return True
    if addr.is_reserved:
        return True
    if addr.is_unspecified:
        return True
    # 其他任何非公开段：RFC 6598 共享地址空间（100.64.0.0/10，含阿里云的
    # 100.100.100.200 元数据端点）既不是 is_private 也不是 is_link_local，上面的
    # 检查会漏掉它。
    if not addr.is_global:
        return True

    # IPv4 映射的 IPv6——检查内嵌的 v4 地址
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return _is_ip_blocked(str(addr.ipv4_mapped))

    # 云元数据（169.254.169.254）
    if ip_str in _METADATA_IPS:
        return True

    return False


# glibc 的 inet_aton 会映射到真实地址、但从 Python 3.9.6 起 ipaddress.ip_address()
# 一律拒绝的主机名字符串：整数（"2130706433" == 127.0.0.1）、十六进制
# （"0x7f000001"）、八进制/前导零点分十进制（"010.0.0.1" == 8.0.0.1）、缩写
# （"127.1" == 127.0.0.1）。只查 ipaddress 的字面 IP 守卫会把这些当作“主机名”
# 放行——glibc 的 getaddrinfo 随后连接到映射出的环回/私有/元数据地址，在
# SSRF_ENABLED 跳过 DNS 解析（默认）时绕过始终开启的字面检查。
_NUMERIC_HOST_RE = re.compile(r"^[0-9a-fA-FxX.]+$")


def _idna_host(host: str) -> str:
    """getaddrinfo 所用 IDNA 编码下，*host* 的 ASCII 形式。

    getaddrinfo 用 IDNA 编码非 ASCII 主机名，把表意/全角点变体映射为 "."，并应用
    NFKC（全角数字变 ASCII）。检查原始字符串会把 "127。0。0。1" 当主机名，而解析器
    实际连接到环回。
    """
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _inet_aton_mapped(host: str) -> str | None:
    """在 inet_aton 语义下，数字 *host* 解析成的点分四组地址。

    inet_aton 接受最多四个点分成分（十进制、前导 0 时为八进制、带 0x 为十六进制），
    最后一个吸收剩余字节："127.1" 是 127.0.0.1，"2130706433" 是 127.0.0.1，
    "2852039166" 是 169.254.169.254（AWS 元数据地址）。当 *host* 不是 inet_aton
    接受的形式时返回 None——即靠自身 DNS 解析的真实主机名。
    """
    if not host:
        return None
    # 在数字形态检查前先解析 IDNA 点/数字变体——对原始扫描它们是“unicode 主机名”，
    # 对解析器却是普通点分四组。
    host = _idna_host(host)
    if len(host) > 64 or not _NUMERIC_HOST_RE.match(host):
        return None
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return None  # inet_aton 拒绝这些；getaddrinfo 随后 DNS 失败——无害
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    components: list[int] = []
    for part in parts:
        try:
            if part.lower().startswith("0x"):
                value = int(part[2:], 16)
            elif len(part) > 1 and part.startswith("0"):
                value = int(part, 8)
            else:
                value = int(part, 10)
        except ValueError:
            return None  # 如 "08.0.0.1"——非法八进制，inet_aton 失败
        if value > 0xFFFFFFFF:
            return None
        components.append(value)
    address = 0
    for value in components[:-1]:
        if value > 0xFF:
            return None  # 非末尾成分是字节——inet_aton 失败
        address = (address << 8) | value
    last = components[-1]
    remaining = 4 - (len(components) - 1)
    if last >= (1 << (8 * remaining)):
        return None
    address = (address << (8 * remaining)) | last
    return str(ipaddress.IPv4Address(address))


def validate_url(url: str, *, allow_any_port: bool = False) -> None:
    """在发出请求前校验 URL 的协议、主机和端口。

    URL 不可安全抓取时抛出 SSRFError。
    此处不解析 DNS——解析后请调用 validate_resolved_ip()。
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"Blocked scheme: {parsed.scheme!r} (allowed: http, https)")

    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no hostname")

    # 拦截元数据主机名。同时检查 IDNA 形式：全角字母会规范化为解析器实际查询的
    # ASCII 名称。
    normalized_host = _idna_host(host)
    if host.lower() in _METADATA_IPS or normalized_host.lower() in _METADATA_IPS:
        raise SSRFError(f"Blocked metadata host: {host}")

    # 检查主机是否为字面 IP 地址
    try:
        ipaddress.ip_address(normalized_host)
        # 它确实是 IP 字面量——进行校验
        if _is_ip_blocked(normalized_host):
            raise SSRFError(f"Blocked IP in URL: {host}")
    except ValueError:
        # 不是规范的 IP 字面量——但仍可能是 ipaddress 拒绝的 inet_aton 替代形式
        # （"2130706433" 是 127.0.0.1，"2852039166" 是 AWS 元数据地址）。glibc 的
        # getaddrinfo 接受这些，因此在 SSRF_ENABLED 跳过 DNS 解析时放行即构成 SSRF
        # 绕过。映射并重新检查。
        mapped = _inet_aton_mapped(host)
        if mapped is not None and _is_ip_blocked(mapped):
            raise SSRFError(f"Blocked IP in URL: {host} (={mapped})")

    # 端口检查——`parsed.port` 在非数字或越界端口（"http://h:abc/"、
    # "http://h:99999/"）时抛 ValueError；调用方只捕获 SSRFError，导致该错误
    # 逸出工具并以晦涩的异常字符串呈现。规范化为结构化错误。
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFError(f"Invalid port in URL: {exc}") from exc
    if port is not None and not allow_any_port and port not in _ALLOWED_PORTS:
        raise SSRFError(f"Blocked port: {port} (allowed: {sorted(_ALLOWED_PORTS)})")


def validate_resolved_ip(ip_str: str, original_url: str = "") -> None:
    """校验解析出的 IP 地址。DNS 解析之后调用。

    IP 位于封禁段时抛出 SSRFError。
    """
    if _is_ip_blocked(ip_str):
        raise SSRFError(
            f"DNS resolved to blocked IP: {ip_str}"
            + (f" (from {original_url})" if original_url else "")
        )


def resolve_and_validate(hostname: str, port: int = 443) -> list[str]:
    """通过 DNS 解析主机名并校验返回的所有 IP。

    返回安全 IP 地址列表。
    所有解析出的 IP 都被封禁，或解析失败时抛出 SSRFError。
    """
    try:
        results = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"DNS resolution failed for {hostname}: {e}")

    if not results:
        raise SSRFError(f"DNS returned no results for {hostname}")

    safe_ips = []
    for family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if not _is_ip_blocked(ip):
            safe_ips.append(ip)

    if not safe_ips:
        blocked = [sockaddr[0] for _, _, _, _, sockaddr in results]
        raise SSRFError(
            f"All resolved IPs for {hostname} are blocked: {blocked}"
        )

    return safe_ips


def validate_redirect(new_url: str, *, allow_any_port: bool = False) -> None:
    """校验重定向目标 URL。规则与 validate_url 相同。"""
    validate_url(new_url, allow_any_port=allow_any_port)
