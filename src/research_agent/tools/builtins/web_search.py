"""内置工具：WebSearch — 搜索网页。

Provider 由 `SEARCH_API_PROVIDER` 决定：

- `duckduckgo`（默认，无需 Key）：优先用 ddgs/duckduckgo-search 库，失败回落 HTML 接口
- `serpapi`：需要 `SEARCH_API_KEY`
- `tavily`：需要 `SEARCH_API_KEY`

需要 Key 的 provider 若未配置 Key，会返回明确的配置错误而不是静默降级 ——
搜索质量直接决定证据质量，静默换源会让用户以为配置生效了。
"""
from __future__ import annotations

import logging
import re

import httpx

from ... import config
from ..registry import default_registry

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_KEYED_PROVIDERS = {"serpapi", "tavily"}
SUPPORTED_PROVIDERS = {"duckduckgo", *_KEYED_PROVIDERS}


def _active_provider() -> str:
    return (config.SEARCH_API_PROVIDER or "duckduckgo").strip().lower()


def _format_results(results: list[dict[str, str]], query: str) -> str:
    if not results:
        return f"No results found for: {query}"
    lines = []
    for index, item in enumerate(results, 1):
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = item.get("snippet") or ""
        lines.append(f"{index}. {title}\n   {url}\n   {snippet}".rstrip())
    return "\n\n".join(lines)


@default_registry.tool(
    name="WebSearch",
    description="Search the web and return results with titles, URLs, and snippets.",
)
async def web_search(query: str, num_results: int = 5) -> str:
    """Search the web using the configured provider.

    Args:
        query: The search query string.
        num_results: Number of results to return (default 5).
    """
    provider = _active_provider()
    if provider not in SUPPORTED_PROVIDERS:
        return (
            f"Error: Unknown SEARCH_API_PROVIDER '{provider}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )
    if provider in _KEYED_PROVIDERS and not config.SEARCH_API_KEY:
        return (
            f"Error: SEARCH_API_PROVIDER is '{provider}' but SEARCH_API_KEY is not set. "
            "Set the key in settings, or switch the provider to 'duckduckgo'."
        )

    try:
        if provider == "serpapi":
            results = await _search_serpapi(query, num_results)
        elif provider == "tavily":
            results = await _search_tavily(query, num_results)
        else:
            return await _search_duckduckgo_with_fallback(query, num_results)
    except httpx.HTTPError as exc:
        logger.warning("%s search failed: %s", provider, exc)
        return f"Error: {provider} search request failed: {exc}"
    except Exception as exc:  # provider 返回体异常
        logger.warning("%s search failed: %s", provider, exc)
        return f"Error: {provider} search failed: {exc}"

    return _format_results(results, query)


# ═══════════════════════════════════════════════════════════════
# SerpAPI
# ═══════════════════════════════════════════════════════════════


async def _search_serpapi(query: str, num_results: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            "https://serpapi.com/search",
            params={
                "q": query,
                "num": num_results,
                "engine": "google",
                "api_key": config.SEARCH_API_KEY,
            },
        )
        response.raise_for_status()
        payload = response.json()

    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))

    results = []
    for item in (payload.get("organic_results") or [])[:num_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
        )
    return results


# ═══════════════════════════════════════════════════════════════
# Tavily
# ═══════════════════════════════════════════════════════════════


async def _search_tavily(query: str, num_results: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "max_results": num_results,
                "search_depth": "advanced",
            },
            headers={"Authorization": f"Bearer {config.SEARCH_API_KEY}"},
        )
        response.raise_for_status()
        payload = response.json()

    results = []
    for item in (payload.get("results") or [])[:num_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
        )
    return results


# ═══════════════════════════════════════════════════════════════
# DuckDuckGo（无 Key，双通道）
# ═══════════════════════════════════════════════════════════════


async def _search_duckduckgo_with_fallback(query: str, num_results: int) -> str:
    try:
        return _format_results(await _search_duckduckgo(query, num_results), query)
    except Exception as exc:
        logger.warning("duckduckgo-search failed: %s", exc)

    try:
        return _format_results(await _search_ddg_html(query, num_results), query)
    except Exception as exc:
        logger.warning("DuckDuckGo HTML search failed: %s", exc)

    return (
        "Error: Web search is not available. Install 'ddgs' "
        "(pip install ddgs), or configure SEARCH_API_PROVIDER=serpapi|tavily "
        "with SEARCH_API_KEY."
    )


async def _search_duckduckgo(query: str, num_results: int) -> list[dict[str, str]]:
    """使用 ddgs / duckduckgo-search 库搜索。"""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for item in ddgs.text(query, max_results=num_results):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("link", "")),
                    "snippet": item.get("body", item.get("snippet", "")),
                }
            )
    return results


async def _search_ddg_html(query: str, num_results: int) -> list[dict[str, str]]:
    """直接用 DuckDuckGo 的 HTML 接口（兜底方案）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (research-agent)"},
        )
        response.raise_for_status()

    results = []
    for match in re.finditer(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', response.text
    ):
        results.append(
            {
                "title": re.sub(r"<[^>]+>", "", match.group(2)),
                "url": match.group(1),
                "snippet": "",
            }
        )
        if len(results) >= num_results:
            break
    return results
