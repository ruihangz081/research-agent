"""内置工具：WebFetch — 抓取网页并提取正文。"""
from __future__ import annotations

import logging
import re

import httpx

from ..registry import default_registry

logger = logging.getLogger(__name__)

_MAX_CONTENT_LEN = 8000  # 截断长度，避免上下文爆炸


@default_registry.tool(name="WebFetch", description="Fetch a web page URL and return its main text content.")
async def web_fetch(url: str) -> str:
    """Fetch a URL and return its content as cleaned text.

    Args:
        url: The URL to fetch.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (research-agent)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching {url}"
    except httpx.RequestError as e:
        return f"Error fetching URL: {e}"

    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        text = _extract_text_from_html(resp.text)
    else:
        text = resp.text

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
