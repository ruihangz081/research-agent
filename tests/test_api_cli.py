from pathlib import Path

import httpx
import asyncio
import pytest

from research_agent.sources.api import create_app


@pytest.mark.anyio
async def test_source_api_upload_read_and_project_boundary(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/projects/project-a/sources", files={"file": ("brief.txt", b"API evidence")})
        assert response.status_code == 200
        payload = response.json()
        source_id = payload["source"]["source_id"]
        listed = await client.get("/api/projects/project-a/sources")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["source_id"] == source_id
        denied = await client.get(f"/api/projects/project-b/sources/{source_id}")
        assert denied.status_code == 404


@pytest.mark.anyio
async def test_batch_upload_rejects_traversal_without_path_access(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/projects/project-a/source-batches", files=[("files", ("../secret.txt", b"no"))])
        assert response.status_code == 200
        assert response.json()["items"][0]["error"]


@pytest.mark.anyio
async def test_source_metadata_can_be_patched_repeatedly(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        uploaded = await client.post("/api/projects/project-a/sources", files={"file": ("brief.txt", b"API evidence")})
        source_id = uploaded.json()["source"]["source_id"]
        first = await client.patch(f"/api/projects/project-a/sources/{source_id}", json={"title": "First"})
        second = await client.patch(f"/api/projects/project-a/sources/{source_id}", json={"title": "Second"})
        assert first.status_code == second.status_code == 200
        assert second.json()["title"] == "Second"


@pytest.mark.anyio
async def test_configured_project_acl_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_API_KEYS_JSON", '{"key-a":["project-a"]}')
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/projects/project-a/sources")
        allowed = await client.get("/api/projects/project-a/sources", headers={"X-Source-API-Key": "key-a"})
        other = await client.get("/api/projects/project-b/sources", headers={"X-Source-API-Key": "key-a"})
        assert denied.status_code == other.status_code == 403
        assert allowed.status_code == 200


@pytest.mark.anyio
async def test_concurrent_metadata_and_activation_preserve_both_changes(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        uploaded = await client.post("/api/projects/project-a/sources", files={"file": ("brief.txt", b"API evidence")})
        source_id = uploaded.json()["source"]["source_id"]
        from research_agent.sources.api import build_runtime
        source_service, _ = build_runtime(tmp_path / "sources")
        source_service.parse_source("project-a", source_id)
        source_service.index_source("project-a", source_id)
        await asyncio.gather(
            client.post(f"/api/projects/project-a/sources/{source_id}/activate"),
            client.patch(f"/api/projects/project-a/sources/{source_id}", json={"title": "Concurrent"}),
        )
        current = (await client.get(f"/api/projects/project-a/sources/{source_id}")).json()["source"]
        assert current["status"] == "active"
        assert current["title"] == "Concurrent"
