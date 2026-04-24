"""内置工具：WebSearch — 搜索网页。

默认使用 DuckDuckGo（零 API Key），可选 SerpAPI/Tavily。
"""
from __future__ import annotations

import logging

import httpx

from ..registry import default_registry

logger = logging.getLogger(__name__)


@default_registry.tool(name="WebSearch", description="Search the web and return results with titles, URLs, and snippets.")
async def web_search(query: str, num_results: int = 5) -> str:
    """Search the web using DuckDuckGo or configured search API.

    Args:
        query: The search query string.
        num_results: Number of results to return (default 5).
    """
    # 尝试用 duckduckgo-search 库（可选依赖）
    try:
        return await _search_duckduckgo(query, num_results)
    except ImportError:
        pass

    # 兜底：用 DuckDuckGo HTML API（无需额外依赖）
    try:
        return await _search_ddg_html(query, num_results)
    except Exception as e:
        logger.warning("DuckDuckGo HTML search failed: %s", e)

    return f"Error: Web search is not available. Install 'duckduckgo-search' package: pip install duckduckgo-search"


async def _search_duckduckgo(query: str, num_results: int) -> str:
    """使用 duckduckgo-search 库搜索。"""
    from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=num_results):
            results.append(r)

    if not results:
        return f"No results found for: {query}"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", r.get("link", ""))
        snippet = r.get("body", r.get("snippet", ""))
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


async def _search_ddg_html(query: str, num_results: int) -> str:
    """直接用 DuckDuckGo 的 HTML 接口（兜底方案）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (research-agent)"},
        )
        resp.raise_for_status()

    # 简易 HTML 提取（不依赖 BeautifulSoup）
    text = resp.text
    results = []
    import re
    # 找 result__a 链接
    for match in re.finditer(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', text
    ):
        url = match.group(1)
        title = re.sub(r"<[^>]+>", "", match.group(2))
        results.append(f"- {title}\n  {url}")
        if len(results) >= num_results:
            break

    if not results:
        return f"No results found for: {query}"
    return "\n".join(results)
