"""网页抓取工具——带 SSRF 防护地从 URL 取回内容。"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from .base import Tool, ToolContext
from ._http import ensure_http_client, validate_outbound_url
from ._ssrf import SSRFError, validate_redirect, validate_url, _MAX_REDIRECTS, _MAX_RESPONSE_BYTES

_log = logging.getLogger(__name__)


class WebFetchTool(Tool):
    name = "web_fetch"
    prompt_hint = (
        "One of the only network paths: fetch a URL's text content (HTTP GET). Shell "
        "curl/wget are blocked — never attempt them. For authenticated or non-GET API "
        "calls use api_request or kong instead."
    )
    description = "Fetch the text content of a URL via HTTP GET."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "timeout_seconds": {"type": "integer", "default": 30},
            "max_chars": {"type": "integer", "default": 50000, "description": "Truncate response at this many characters"},
        },
        "required": ["url"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = params["url"]

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return {"error": "Invalid URL: must start with http:// or https://"}

        # LLM 提供的参数远比模式暗示的更常是字符串（"30"、"50000"）。字符串超时
        # 以逃过 RequestError 处理器的 TypeError 撞上 httpx.Timeout，字符串
        # max_chars 则让切片崩溃。像 shell 一样钳制两者（超时 1-120 秒；
        # max_chars 1-20 万）。
        try:
            timeout = int(params.get("timeout_seconds", 30))
        except (TypeError, ValueError):
            timeout = 30
        timeout = max(1, min(timeout, 120))
        try:
            max_chars = int(params.get("max_chars", 50000))
        except (TypeError, ValueError):
            max_chars = 50000
        max_chars = max(1, min(max_chars, 200_000))

        # SSRF：校验初始 URL。validate_url 是始终开启的字面检查（方案/主机/IP/
        # 端口/元数据）；validate_outbound_url 在 SSRF_ENABLED 环境门后加 DNS 解析
        # 检查。
        try:
            validate_url(url)
            validate_outbound_url(url)
        except SSRFError as e:
            return {"error": f"Blocked by SSRF policy: {e}"}

        http = ensure_http_client(context, target_url=url)

        try:
            # 手动重定向循环——每一跳重新校验
            current_url = url
            redirects = 0

            while True:
                async with http.stream(
                    "GET",
                    current_url,
                    timeout=timeout,
                    headers={"User-Agent": "AgentsUniverse/1.0"},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        redirects += 1
                        if redirects > _MAX_REDIRECTS:
                            return {"error": f"Too many redirects (max {_MAX_REDIRECTS})"}

                        location = response.headers.get("location", "")
                        if not location:
                            return {"error": "Redirect with no Location header"}

                        # 解析相对重定向（路径相对、仅查询、协议相对）
                        from urllib.parse import urljoin, urlparse
                        resolved_location = urljoin(current_url, location)
                        parsed = urlparse(resolved_location)
                        if parsed.scheme not in ("http", "https"):
                            return {"error": f"Blocked redirect to non-http URL: {location}"}
                        location = resolved_location

                        # SSRF：校验重定向目标
                        try:
                            validate_redirect(location)
                            validate_outbound_url(location)
                        except SSRFError as e:
                            return {"error": f"Redirect blocked by SSRF policy: {e}"}

                        current_url = location
                        continue

                    # 非重定向响应——以硬性大小限制读取正文。content-length 只是
                    # 快速路径预检；分块或无长度正文通过流式读取并在超过
                    # _MAX_RESPONSE_BYTES 时中止来强制，而非整体缓冲。
                    content_length = response.headers.get("content-length")
                    try:
                        clen = int(content_length) if content_length else 0
                    except (TypeError, ValueError):
                        clen = 0
                    if clen > _MAX_RESPONSE_BYTES:
                        return {"error": f"Response too large: {content_length} bytes (max {_MAX_RESPONSE_BYTES})"}

                    total = 0
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            return {"error": f"Response too large (exceeded {_MAX_RESPONSE_BYTES} bytes); download aborted"}
                        chunks.append(chunk)
                    text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

                    content = text[:max_chars]
                    result = {
                        "content": content,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type", ""),
                        "url": current_url,
                        "truncated": len(text) > max_chars,
                    }
                    if redirects > 0:
                        result["redirects"] = redirects
                    if response.status_code >= 400:
                        _log.warning("web_fetch HTTP %d: %s", response.status_code, current_url)
                        return {"error": f"HTTP {response.status_code}", "url": current_url, "status_code": response.status_code}
                    return result

        except httpx.TimeoutException:
            _log.warning("web_fetch timeout after %ds: %s", timeout, url)
            return {"error": f"Request timed out after {timeout}s: {url}"}
        except httpx.RequestError as e:
            _log.warning("web_fetch request error for %s: %s", url, e)
            return {"error": f"Request failed: {e}"}
