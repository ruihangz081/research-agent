"""网页工作台访问控制测试（backlog 第 8 项）。

原问题：--host 可配为 0.0.0.0，一旦暴露，任何人都能读取调研数据、
改模型配置（含写入 .env）、删除项目。
"""
import httpx
import pytest

from research_agent import config, web_app


@pytest.mark.anyio
async def test_no_token_configured_keeps_local_access_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置令牌时不拦截——本机使用不应被打扰。"""
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/config")).status_code == 200


@pytest.mark.anyio
async def test_request_without_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/config")

    assert response.status_code == 401
    assert "令牌" in response.json()["detail"]


@pytest.mark.anyio
async def test_wrong_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/config", headers={web_app.AUTH_HEADER: "wrong"}
        )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_header_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/config", headers={web_app.AUTH_HEADER: "secret-token"}
        )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_query_token_sets_cookie_for_later_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带 ?token= 首访后写 cookie，浏览器后续无需再带参数。"""
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/config?token=secret-token")
        assert first.status_code == 200
        assert client.cookies.get(web_app.AUTH_COOKIE) == "secret-token"

        # 后续请求不带查询参数也能通过（靠 cookie）
        second = await client.get("/api/config")
        assert second.status_code == 200


@pytest.mark.anyio
async def test_mutating_endpoints_are_also_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写操作同样受保护，尤其是会写 .env 的配置接口。"""
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        model = await client.put(
            "/api/config/model",
            json={"base_url": "https://evil.example/v1", "model": "x"},
        )
        delete = await client.request("DELETE", "/api/projects/whatever")

    assert model.status_code == 401
    assert delete.status_code == 401


def test_loopback_detection() -> None:
    assert web_app._is_loopback("127.0.0.1") is True
    assert web_app._is_loopback("localhost") is True
    assert web_app._is_loopback("::1") is True
    assert web_app._is_loopback("0.0.0.0") is False
    assert web_app._is_loopback("192.168.1.10") is False
    assert web_app._is_loopback("example.com") is False


def test_main_refuses_insecure_public_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """无令牌绑定非回环地址时拒绝启动，而不是静默暴露。"""
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr("sys.argv", ["research-agent-web", "--host", "0.0.0.0"])

    with pytest.raises(SystemExit) as excinfo:
        web_app.main()

    assert "WEB_AUTH_TOKEN" in str(excinfo.value)


def test_main_allows_public_bind_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr("sys.argv", ["research-agent-web", "--host", "0.0.0.0"])
    started: dict[str, object] = {}

    def fake_run(app_path, **kwargs):
        started.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    web_app.main()

    assert started["host"] == "0.0.0.0"


def test_main_allows_explicit_insecure_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(
        "sys.argv",
        ["research-agent-web", "--host", "0.0.0.0", "--allow-insecure-host"],
    )
    started: dict[str, object] = {}

    monkeypatch.setattr("uvicorn.run", lambda app_path, **kwargs: started.update(kwargs))
    web_app.main()

    assert started["host"] == "0.0.0.0"
