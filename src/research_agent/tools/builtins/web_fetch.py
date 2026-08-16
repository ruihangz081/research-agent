"""内置工具：WebFetch — 抓取网页并提取正文。"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import ipaddress
import logging
from pathlib import Path
import re
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

import certifi
import httpx

from ..registry import default_registry

logger = logging.getLogger(__name__)

_MAX_CONTENT_LEN = 8000  # 截断长度，避免上下文爆炸
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

# 共享连接池：抓取网页需要 follow_redirects 与受信任 SSL 上下文，单独维护一个
# 模块级 client，避免每次 fetch 都重建连接。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """返回模块级共享 AsyncClient（follow_redirects + 受信任证书），惰性创建。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            verify=_trusted_ssl_context(),
            headers={"User-Agent": "Mozilla/5.0 (research-agent)"},
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
    return _client


def _reset_client() -> None:
    """丢弃共享 client，让下次调用重建（测试隔离或运行时切换配置后调用）。

    进程级 client 随进程退出释放；这里只断开引用，测试用 MockTransport
    不持有真实连接，无需显式 await close。
    """
    global _client
    _client = None


@dataclass(frozen=True)
class WebResource:
    requested_url: str
    final_url: str
    content_type: str
    content: bytes
    text: str


def _validate_public_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public http/https URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not supported")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("local URLs are not supported")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("private or local IP URLs are not supported")


@lru_cache(maxsize=1)
def _trusted_ssl_context() -> ssl.SSLContext:
    """Use certifi plus macOS-managed roots without disabling verification."""
    context = ssl.create_default_context(cafile=certifi.where())
    if sys.platform != "darwin":
        return context
    keychains = (
        Path.home() / "Library/Keychains/login.keychain-db",
        Path("/Library/Keychains/System.keychain"),
        Path("/System/Library/Keychains/SystemRootCertificates.keychain"),
    )
    for keychain in keychains:
        if not keychain.exists():
            continue
        result = subprocess.run(
            ["security", "find-certificate", "-a", "-p", str(keychain)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and "BEGIN CERTIFICATE" in result.stdout:
            context.load_verify_locations(cadata=result.stdout)
    return context


async def fetch_web_resource(url: str) -> WebResource:
    """Fetch one public web resource without truncating its immutable snapshot."""
    _validate_public_http_url(url)
    resp = await _get_client().get(url)
    resp.raise_for_status()
    _validate_public_http_url(str(resp.url))
    content = resp.content
    if len(content) > _MAX_DOWNLOAD_BYTES:
        raise ValueError(f"web resource exceeds {_MAX_DOWNLOAD_BYTES} bytes")
    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    text = _extract_text_from_html(resp.text) if content_type in {"text/html", "application/xhtml+xml"} else resp.text
    return WebResource(
        requested_url=url,
        final_url=str(resp.url),
        content_type=content_type,
        content=content,
        text=text,
    )


@default_registry.tool(name="WebFetch", description="Fetch a web page URL and return its main text content.")
async def web_fetch(url: str) -> str:
    """Fetch a URL and return its content as cleaned text.

    Args:
        url: The URL to fetch.
    """
    try:
        resource = await fetch_web_resource(url)
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching {url}"
    except httpx.RequestError as e:
        return f"Error fetching URL: {e}"
    except ValueError as e:
        return f"Error fetching URL: {e}"

    text = resource.text
    if len(text) > _MAX_CONTENT_LEN:
        text = text[:_MAX_CONTENT_LEN] + f"\n\n[... truncated, total {len(text)} chars]"

    return text if text.strip() else f"No readable content extracted from {url}"


def _extract_text_from_html(html: str) -> str:
    """从 HTML 提取正文。优先用 BeautifulSoup，兜底用正则。"""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        # 移除 script/style/nav/header/footer
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 合并连续空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text
    except ImportError:
        pass

    # 兜底：正则剥离 HTML 标签
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
