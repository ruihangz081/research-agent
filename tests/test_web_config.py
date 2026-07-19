from pathlib import Path

import httpx
import pytest

from research_agent import config
from research_agent import web_app


@pytest.mark.anyio
async def test_model_config_can_be_saved_without_exposing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("SEARCH_API_PROVIDER=duckduckgo\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MODEL", "gpt-4o")
    monkeypatch.setattr(config, "DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setattr(config, "LLM_TIMEOUT", 120.0)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.7)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_TIMEOUT", "120")
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/model",
            json={
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-secret-key",
                "model": "deepseek-chat",
                "timeout": 60,
                "max_retries": 2,
                "temperature": 0.4,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "deepseek-chat"
    assert payload["has_api_key"] is True
    assert "api_key" not in payload
    saved = env_path.read_text(encoding="utf-8")
    assert "LLM_API_KEY=test-secret-key" in saved
    assert "SEARCH_API_PROVIDER=duckduckgo" in saved


@pytest.mark.anyio
async def test_model_config_rejects_non_http_base_url() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/model",
            json={
                "base_url": "file:///tmp/model",
                "model": "local-model",
            },
        )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_workspace_config_can_be_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    projects_dir = tmp_path / "projects"
    source_dir = tmp_path / "sources"
    projects_dir.mkdir()
    source_dir.mkdir()
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "MAX_COLLECT_ROUNDS", 3)
    monkeypatch.setattr(config, "OUTPUT_PREFERENCE", "balanced")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "old-projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "active-sources")
    monkeypatch.delenv("SOURCE_DATA_DIR", raising=False)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/workspace",
            json={
                "default_rounds": 5,
                "output_preference": "deep",
                "projects_dir": str(projects_dir),
                "source_data_dir": str(source_dir),
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_rounds"] == 5
    assert payload["output_preference"] == "deep"
    assert payload["projects_dir"] == str(projects_dir)
    assert payload["source_data_dir"] == str(source_dir)
    assert payload["source_restart_required"] is True
    saved = env_path.read_text(encoding="utf-8")
    assert "MAX_COLLECT_ROUNDS=5" in saved
    assert "OUTPUT_PREFERENCE=deep" in saved
    assert f"PROJECTS_DIR={projects_dir}" in saved
    assert f"SOURCE_DATA_DIR={source_dir}" in saved


@pytest.mark.anyio
async def test_workspace_config_rejects_relative_paths() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/workspace",
            json={
                "default_rounds": 3,
                "output_preference": "balanced",
                "projects_dir": "projects",
                "source_data_dir": ".data/sources",
            },
        )

    assert response.status_code == 400
