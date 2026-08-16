"""WebSearch provider 路由测试（backlog 第 4 项）。

原问题：SEARCH_API_PROVIDER / SEARCH_API_KEY 在配置里定义、在 README 承诺，
但搜索代码从未读取，用户填了 Key 也不生效。
"""
import importlib
import json

import httpx
import pytest

from research_agent import config

# builtins/__init__ 里的 `from .web_search import web_search` 会用同名函数遮蔽
# 子模块属性，所以 `import ... as ws` 拿到的是函数。用 importlib 取模块本身。
ws = importlib.import_module("research_agent.tools.builtins.web_search")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("api_key", "expected_authorization"),
    [("anysearch-key", "Bearer anysearch-key"), ("", None)],
)
async def test_anysearch_results_and_optional_auth(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
    expected_authorization: str | None,
) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "anysearch")
    monkeypatch.setattr(config, "SEARCH_API_KEY", api_key)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "results": [
                        {
                            "title": "权威来源",
                            "url": "https://example.gov.cn/report",
                            "snippet": "2025 年官方数据",
                            "content": "搜索正文不能绕过项目证据入库流程。",
                        }
                    ]
                },
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ws, "_client", httpx.AsyncClient(transport=transport))

    result = await ws.web_search("2025 官方数据", num_results=3)

    assert captured["authorization"] == expected_authorization
    assert captured["payload"] == {
        "query": "2025 官方数据",
        "max_results": 3,
        "format": "json",
    }
    assert "权威来源" in result
    assert "2025 年官方数据" in result
    assert "不能绕过" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("failure_mode", ["error", "empty"])
async def test_anysearch_failure_or_empty_results_fall_back_to_duckduckgo(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "anysearch")

    async def unavailable_anysearch(query, num_results):
        calls.append("anysearch")
        if failure_mode == "error":
            raise httpx.TimeoutException("timed out")
        return []

    async def working_ddg(query, num_results):
        calls.append("duckduckgo")
        return "DDG fallback result"

    monkeypatch.setattr(ws, "_search_anysearch", unavailable_anysearch)
    monkeypatch.setattr(ws, "_search_duckduckgo_with_fallback", working_ddg)

    assert await ws.web_search("任意查询") == "DDG fallback result"
    assert calls == ["anysearch", "duckduckgo"]


@pytest.mark.anyio
async def test_unknown_provider_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "bing")
    result = await ws.web_search("任意查询")
    assert result.startswith("Error:")
    assert "bing" in result


@pytest.mark.anyio
async def test_keyed_provider_without_key_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 Key 时必须明确报错，而不是静默退回 DuckDuckGo。"""
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "serpapi")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")

    called = False

    async def fake_ddg(query, num_results):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(ws, "_search_duckduckgo", fake_ddg)

    result = await ws.web_search("任意查询")

    assert result.startswith("Error:")
    assert "SEARCH_API_KEY" in result
    assert called is False


@pytest.mark.anyio
async def test_serpapi_results_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "serpapi")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "test-key")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "半导体行业年报",
                        "link": "https://example.gov.cn/report",
                        "snippet": "2025 年营业收入增长 18%",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ws, "_client", httpx.AsyncClient(transport=transport))

    result = await ws.web_search("半导体 营业收入", num_results=1)

    assert "半导体行业年报" in result
    assert "https://example.gov.cn/report" in result
    assert "serpapi.com" in str(captured["url"])
    assert "test-key" in str(captured["url"])


@pytest.mark.anyio
async def test_tavily_results_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "tavily")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "tv-key")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "行业研报",
                        "url": "https://example.com/a",
                        "content": "市场规模 42 亿元",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ws, "_client", httpx.AsyncClient(transport=transport))

    result = await ws.web_search("行业 市场规模", num_results=1)

    assert "行业研报" in result
    assert "市场规模 42 亿元" in result
    assert captured["auth"] == "Bearer tv-key"


@pytest.mark.anyio
async def test_provider_http_error_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "tavily")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "tv-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid key"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ws, "_client", httpx.AsyncClient(transport=transport))

    result = await ws.web_search("任意查询")

    assert result.startswith("Error:")
    assert "tavily" in result


@pytest.mark.anyio
async def test_duckduckgo_falls_back_to_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 provider 的库通道失败时回落 HTML 通道。"""
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "duckduckgo")

    async def failing_lib(query, num_results):
        raise RuntimeError("ddgs unavailable")

    async def working_html(query, num_results):
        return [{"title": "兜底结果", "url": "https://example.com", "snippet": ""}]

    monkeypatch.setattr(ws, "_search_duckduckgo", failing_lib)
    monkeypatch.setattr(ws, "_search_ddg_html", working_html)

    result = await ws.web_search("任意查询")

    assert "兜底结果" in result


@pytest.mark.anyio
async def test_empty_results_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "duckduckgo")

    async def empty(query, num_results):
        return []

    monkeypatch.setattr(ws, "_search_duckduckgo", empty)

    assert "No results found" in await ws.web_search("查不到的东西")
